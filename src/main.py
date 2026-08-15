import os
import json
import shutil
import asyncio
import sys

# La consola de Windows sin UTF-8 usa cp1252, que no sabe escribir los emojis
# de nuestros `print` de progreso: el print lanza UnicodeEncodeError y, como
# ocurre dentro del pipeline, MATA LA CORRIDA ENTERA. Nos tumbo un analisis en
# el nodo 12, tras 98 segundos de trabajo ya hecho.
#
# Se arregla aca, en el punto de entrada, y no quitando los emojis de cada
# print: los prints se siguen escribiendo y el proximo emoji volveria a
# reventar. Con `errors="replace"` lo peor que pasa es que un caracter salga
# como "?" en una consola vieja, que es exactamente lo que un mensaje de
# progreso puede permitirse.
#
# En Docker no se nota porque ahi la salida ya es UTF-8; solo aparece al correr
# el core nativo en Windows.
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # flujo redirigido o sin soporte: no vale la pena tumbar el arranque

from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, File, UploadFile, Form, HTTPException
from sqlmodel import SQLModel, Session
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from utils.database import engine, Job, migrar
from utils.services import run_pipeline_task, TEMP_VIDEOS
from utils import malla as malla_utils

# Los videos y los JSON de resultado van bajo DATA_DIR, que es el directorio
# montado como volumen. La URL publica sigue siendo /temp_videos para no
# romper al frontend, que ya la usa para leer la mascara de cambios.
os.makedirs(TEMP_VIDEOS, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Inicializando recursos de la aplicación...")
    SQLModel.metadata.create_all(engine)
    migrar()   # columnas nuevas sobre una base que ya existe
    yield 
    print("Apagando la aplicación y liberando recursos...")
    engine.dispose()

app = FastAPI(title="API de Análisis Flyrocks", lifespan=lifespan)

app.mount("/temp_videos", StaticFiles(directory=TEMP_VIDEOS), name="temp_videos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En desarrollo permitimos todo. En prod, pones la URL de tu front
    allow_credentials=True,
    allow_methods=["*"],  # Permite POST, GET, OPTIONS, etc.
    allow_headers=["*"],
)

def _fps_de(video_path: str):
    """FPS del video, o None si no se puede leer.

    Hace falta para convertir el tiempo de detonación (ms en el CSV) a frame del
    video, que es la unidad en la que vienen las trayectorias. Se lee acá y no
    en el pipeline porque acá el archivo ya está en disco y cuesta milisegundos.
    """
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        return round(float(fps), 3) if fps and fps > 0 else None
    except Exception:
        return None


# --- ENDPOINT PARA DISPARAR EL ANÁLISIS ---
# Cambiamos la ruta a /api/analyze para que haga match con el fetch del JS
@app.post("/api/analyze")
async def start_analysis(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    origin_zone: str = Form(...),
    expected_projection_zone: str = Form(...),
    h_matrix: str = Form(...),
    percentile: float = Form(..., ge=0.0, le=100.0),
    sigma: float = Form(..., ge=0.0, le=1.0),
    esp: float = Form(..., ge=1.0, le=7.0),
    # El CSV de secuencia (Label, X, Y, Z, DetonatingTime). Es OPCIONAL: sin él
    # el análisis corre igual y produce las mismas trayectorias, solo que el job
    # queda sin malla y ninguna vista puede decir de qué tiro salió cada roca.
    detonation_sequence: UploadFile = File(None),
    # El ancla temporal, en frames del VIDEO ORIGINAL. Los dos son opcionales.
    #
    # El CSV de secuencia da tiempos RELATIVOS entre pozos, no el frame en que
    # arranca la secuencia dentro del clip. Sin ese origen no se puede cruzar el
    # nacimiento de una traza con la detonación de un pozo, y el calce temporal
    # queda dependiendo de un slider que el usuario mueve a ojo.
    #
    # El número ya existe aguas arriba y hasta ahora se tiraba: el blast
    # detector detecta la detonación y el usuario elige dónde cortar. Los dos
    # viven en el navegador (paso 2) y morían ahí.
    frame_detonacion: int = Form(None),   # lo que detectó el blast detector
    frame_inicio_corte: int = Form(None), # dónde cortó el usuario
):
    # 1. Parsear y validar los strings JSON que vienen del form
    try:
        origin_zone_parsed = json.loads(origin_zone)
        expected_zone_parsed = json.loads(expected_projection_zone)
        h_matrix_parsed = json.loads(h_matrix)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Los parámetros de zonas o matriz deben ser JSON válidos.")

    # 2. Guardar el archivo de video temporalmente en disco
    # Esto es necesario porque run_pipeline_task probablemente necesite un 'path' físico
    video_path = f"{TEMP_VIDEOS}/{video.filename}"
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    # 3. Creamos el registro en la DB, guardando CON QUÉ se corrió.
    # Sin esto el análisis queda huérfano de su contexto: se puede saber qué
    # trayectorias salieron, pero no sobre qué homografía ni qué zonas, que es
    # lo que cualquier vista necesita para dibujar la malla o asociar al tiro.
    entrada = {
        "video": video.filename,
        "h_matrix": h_matrix_parsed,
        "origin_zone": origin_zone_parsed,
        "expected_projection_zone": expected_zone_parsed,
        "parametros": {"percentile": percentile, "sigma": sigma, "esp": esp},
    }

    # El ancla sale de restar los dos, pero se guardan LOS TRES: el ancla es lo
    # que la vista usa, y los crudos permiten recalcularla si mañana cambia el
    # criterio, sin volver a pedirle nada al usuario. Mismo principio que
    # guardar el CSV crudo además de la malla proyectada.
    #
    # El resultado es el frame del CLIP en que ocurre la primera detonación, que
    # es el origen al que el CSV de secuencia le suma sus tiempos relativos.
    # Si el usuario acepta la sugerencia del detector tal cual, da ~6: el blast
    # detector reporta a propósito unos frames antes de donde dispara, para que
    # el corte no se coma el destello.
    if frame_detonacion is not None and frame_inicio_corte is not None:
        entrada["recorte"] = {
            "frame_detonacion": frame_detonacion,
            "frame_inicio_corte": frame_inicio_corte,
            "ancla_frames": frame_detonacion - frame_inicio_corte,
        }
        print(f"[ancla] {frame_detonacion} - {frame_inicio_corte} = "
              f"{frame_detonacion - frame_inicio_corte} frames")

    # La malla de tiros, si vino el CSV. Se guardan las dos cosas a propósito:
    # el texto crudo es la fuente de verdad (pesa ~5 KB y deja el job
    # autocontenido: si mañana cambia la forma de proyectar, se reproyecta sin
    # pedirle el archivo de nuevo al usuario) y `malla` es el resultado ya
    # proyectado, listo para dibujar.
    #
    # Un CSV mal formado NO tumba el análisis: se anota el error en `entrada` y
    # el pipeline sigue. Perder la asociación es malo; perder también las
    # trayectorias por una fila rara de Excel sería peor.
    if detonation_sequence is not None and detonation_sequence.filename:
        crudo = await detonation_sequence.read()
        entrada["secuencia"] = {
            "archivo": detonation_sequence.filename,
            "csv": crudo.decode("utf-8-sig", errors="replace"),
        }
        try:
            entrada["malla"] = malla_utils.desde_csv(
                crudo, h_matrix_parsed, fps=_fps_de(video_path)
            )
            print(f"[malla] {entrada['malla']['meta']['n_pozos']} pozos proyectados")
        except Exception as e:
            entrada["malla_error"] = str(e)
            print(f"[malla] no se pudo procesar el CSV: {e}")
    with Session(engine) as session:
        new_job = Job(status="Iniciando...", progress=0, entrada=entrada)
        session.add(new_job)
        session.commit()
        session.refresh(new_job)
    
    # 4. Enviamos la tarea pesada a segundo plano
    # IMPORTANTE: Asegúrate de que `run_pipeline_task` acepte estos nuevos parámetros en utils/services.py
    background_tasks.add_task(
        run_pipeline_task, 
        new_job.id, 
        video_path, 
        origin_zone_parsed, 
        expected_zone_parsed, 
        h_matrix_parsed,
        percentile,
        sigma,
        esp,    
        output_filename="voladura_analisis.mp4"  
    )
    
    return {"job_id": new_job.id, "mensaje": "Análisis encolado en segundo plano"}

# --- WEBSOCKET PARA NOTIFICAR EL AVANCE ---
@app.websocket("/ws/progress/{job_id}")
async def websocket_job_status(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            job_data = None
            
            # --- BLOQUE 1: Leer la Base de Datos con cuidado ---
            try:
                with Session(engine) as session:
                    job = session.get(Job, job_id)
                    if job:
                        job_data = {
                            "id": job.id,
                            "status": job.status,
                            "percentage": job.progress,
                            "is_running": job.is_running,
                            "result_file_path": job.result_file_path,
                            "error_message": job.error_message,
                            "has_report": False
                        }
            except Exception as db_error:
                # Solo atrapamos errores de SQLite aquí
                print(f"⏳ Base de datos ocupada. Reintentando...")
                await asyncio.sleep(1)
                continue  # Volvemos al inicio del while

            # Si el job_id no existe en la base de datos
            if not job_data:
                await websocket.send_json({"error": "Job no encontrado"})
                break
            
            # --- BLOQUE 2: Enviar los datos al Frontend ---
            # Si el frontend se desconectó, esto lanzará un error que romperá el while
            await websocket.send_json(job_data)

            # Si el proceso terminó con éxito o error, cerramos el bucle
            if not job_data["is_running"]:
                break
                
            # Esperamos 1 segundo antes de la próxima actualización
            await asyncio.sleep(1)
            
        # Si salimos del bucle limpiamente, cerramos la conexión
        await websocket.close()
        
    except WebSocketDisconnect:
        print(f"🔌 Cliente desconectado normalmente del job {job_id}")
    except RuntimeError as e:
        print(f"🔌 Conexión cerrada inesperadamente: {str(e)}")
    except Exception as e:
        print(f"❌ Error inesperado en el WebSocket: {str(e)}")
        
@app.get("/api/results/{job_id}")
def get_job_results(job_id: str):
    with Session(engine) as session:
        # Buscamos el registro en la base de datos usando el UUID
        job = session.get(Job, job_id)
        
        if not job:
            # Si no existe, devolvemos un error 404 (Not Found)
            raise HTTPException(status_code=404, detail="Análisis no encontrado")
        
        # FastAPI automáticamente convierte el modelo Job de SQLModel a JSON
        return job

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)