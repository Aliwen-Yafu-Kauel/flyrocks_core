import os
import json
import shutil
import asyncio
import sys
import time
import uuid
from pathlib import Path
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,          
)

for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  

from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, File, UploadFile, Form, HTTPException
from pydantic import BaseModel
from sqlmodel import SQLModel, Session
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from utils.database import engine, Job, migrar
from utils.services import run_pipeline_task, TEMP_VIDEOS
from utils import malla as malla_utils


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

class _EstaticoSinCache(StaticFiles):

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp


app.mount("/temp_videos", _EstaticoSinCache(directory=TEMP_VIDEOS), name="temp_videos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En desarrollo permitimos todo. En prod, pones la URL de tu front
    allow_credentials=True,
    allow_methods=["*"],  # Permite POST, GET, OPTIONS, etc.
    allow_headers=["*"],
)

class ResumeRequest(BaseModel):
    percentile: float

def _guardar_artefactos(carpeta: Path, job_id: str, nombre_video: str, ancla):

    import cv2

    ruta_video = carpeta / nombre_video
    destino = carpeta / "frame.jpg"
    try:
        cap = cv2.VideoCapture(str(ruta_video))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # Tres frames antes del primer tiro; si no hay ancla, el primero del clip
        # (que tambien es pre-tronadura, porque el corte empieza antes).
        objetivo = max(0, (ancla or 3) - 3)
        if total and objetivo >= total:
            objetivo = 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, objetivo)
        ok, frame = cap.read()
        if not ok:                      # algunos codecs no aceptan el salto
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        cap.release()
        if ok:
            cv2.imwrite(str(destino), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            print(f"[frame] referencia del frame {objetivo} -> {destino.name}")
    except Exception as e:              # no vale la pena tumbar el analisis
        print(f"[frame] no se pudo extraer el frame de referencia: {e}")

    web = _derivar_video_web(carpeta, nombre_video)

    return {
        "carpeta": job_id,
        "mascara": f"{job_id}/mascara_cambios.png",
        "frame": f"{job_id}/frame.jpg" if destino.exists() else None,
        # El derivado reproducible si se pudo lanzar; si no, el original, que al
        # menos sirve para descargarlo aunque el navegador no lo pinte.
        "video": f"{job_id}/{web}" if web else f"{job_id}/{nombre_video}",
        "video_original": f"{job_id}/{nombre_video}",
    }


# Códecs que un navegador reproduce de verdad. El resto hay que convertirlo.
_CODECS_WEB = ("avc1", "h264")


def _derivar_video_web(carpeta: Path, nombre_video: str):
    import shutil as _sh
    import subprocess
    import threading

    origen = carpeta / nombre_video
    if not origen.exists():
        return None

    # Si ya viene en H.264 no se toca: transcodificar de nuevo solo perderia
    # calidad y tiempo.
    try:
        import cv2
        cap = cv2.VideoCapture(str(origen))
        cc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        cap.release()
        fourcc = "".join(chr((cc >> 8 * i) & 0xFF) for i in range(4)).lower()
        if fourcc in _CODECS_WEB:
            print(f"[video] {nombre_video} ya es {fourcc}: no hace falta derivado")
            return None
    except Exception as e:
        fourcc = "?"
        print(f"[video] no se pudo leer el codec ({e}); se genera derivado igual")

    if not _sh.which("ffmpeg"):
        print("[video] sin ffmpeg: el fondo de video no se va a poder reproducir")
        return None

    salida = "video_web.mp4"
    # Se escribe a un temporal y se renombra al final. Si no, la vista puede
    # pedir el archivo a medio escribir y fallar igual que antes, pero ahora sin
    # que se entienda por que.
    tmp = carpeta / "video_web.parcial.mp4"
    final = carpeta / salida

    def convertir():
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", str(origen),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            # GOP corto: la vista salta frame a frame y con keyframes cada 250
            # cuadros cada salto obliga a decodificar medio segundo de video.
            "-g", "15",
            "-pix_fmt", "yuv420p",
            # El moov al principio. OpenCV lo deja al final, y asi el navegador
            # tiene que bajarse el archivo entero antes de pintar el primer
            # cuadro (92 MB por un frame).
            "-movflags", "+faststart",
            "-an", str(tmp),
        ]
        try:
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if r.returncode == 0 and tmp.exists():
                tmp.replace(final)
                mb = final.stat().st_size / 1e6
                print(f"[video] derivado H.264 listo en {time.time()-t0:.0f}s "
                      f"({mb:.0f} MB, desde {fourcc})")
            else:
                print(f"[video] ffmpeg fallo ({r.returncode}): "
                      f"{(r.stderr or '').strip()[:300]}")
                tmp.unlink(missing_ok=True)
        except Exception as e:
            print(f"[video] no se pudo derivar el clip: {e}")
            tmp.unlink(missing_ok=True)

    threading.Thread(target=convertir, daemon=True).start()
    print(f"[video] {nombre_video} viene en {fourcc}: derivando H.264 en segundo plano")
    return salida


def _fps_de(video_path: str):

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
    detonation_sequence: UploadFile = File(None),
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

    
    job_id = str(uuid.uuid4())
    carpeta = Path(TEMP_VIDEOS) / job_id
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_video = Path(video.filename or "video").name or "video"

    if not Path(nombre_video).suffix:
        nombre_video += ".mp4"
    video_path = str(carpeta / nombre_video)
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    entrada = {
        "video": video.filename,
        "h_matrix": h_matrix_parsed,
        "origin_zone": origin_zone_parsed,
        "expected_projection_zone": expected_zone_parsed,
        "parametros": {"percentile": percentile, "sigma": sigma, "esp": esp},
    }


    if frame_detonacion is not None and frame_inicio_corte is not None:
        entrada["recorte"] = {
            "frame_detonacion": frame_detonacion,
            "frame_inicio_corte": frame_inicio_corte,
            "ancla_frames": frame_detonacion - frame_inicio_corte,
        }

        print(f"[ancla] {frame_detonacion} - {frame_inicio_corte} = "
              f"{frame_detonacion - frame_inicio_corte} frames")


    entrada["artefactos"] = _guardar_artefactos(
        carpeta, job_id, nombre_video,
        entrada.get("recorte", {}).get("ancla_frames"))


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
        new_job = Job(id=job_id, status="Iniciando...", progress=0, entrada=entrada)
        session.add(new_job)
        session.commit()
        session.refresh(new_job)

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

# --- ENDPOINT PARA REANUDAR TRAS LA SELECCIÓN DE PERCENTIL ---
@app.post("/api/resume/{job_id}")
async def resume_analysis(
    job_id: str,
    body: ResumeRequest,
    background_tasks: BackgroundTasks
):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Análisis no encontrado")
        
        if job.status != "ESPERANDO_PERCENTIL_USUARIO":
            raise HTTPException(status_code=400, detail=f"El job no está en estado de pausa. Estado actual: {job.status}")

        entrada = job.entrada or {}
        parametros = entrada.get("parametros", {})
        parametros["percentile"] = body.percentile
        entrada["parametros"] = parametros
        
        job.entrada = entrada
        job.status = "Reanudando pipeline con nuevo percentil..."
        session.add(job)
        session.commit()

        video_path = os.path.join(TEMP_VIDEOS, job_id, f"{entrada.get('video', 'video.mp4')}")
        if not os.path.exists(video_path):
            video_path = os.path.join(TEMP_VIDEOS, job_id, "video.mp4")

        background_tasks.add_task(
            run_pipeline_task,
            job_id,
            video_path,
            entrada.get("origin_zone", []),
            entrada.get("expected_projection_zone", []),
            entrada.get("h_matrix", []),
            body.percentile,
            parametros.get("sigma", 0.5),
            parametros.get("esp", 5.0)
        )

    return {"job_id": job_id, "mensaje": f"Pipeline reanudado con percentil {body.percentile}%"}

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
        
@app.get("/api/jobs")
def list_jobs(limite: int = 50):

    from sqlalchemy import text

    with Session(engine) as session:
        filas = session.execute(text("""
            SELECT id, creado_en, status, is_running,
                   json_extract(entrada, '$.video')             AS video,
                   json_extract(entrada, '$.artefactos.carpeta') AS carpeta,
                   CASE WHEN json_data IS NULL THEN 0
                        ELSE (SELECT count(*) FROM json_each(job.json_data)) END AS trayectorias
            FROM job
            ORDER BY creado_en DESC, rowid DESC
            LIMIT :limite
        """), {"limite": limite}).mappings().all()

    return [dict(f) for f in filas]


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