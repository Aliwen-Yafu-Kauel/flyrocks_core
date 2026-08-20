import os
import json
import shutil
import asyncio
import sys
import time
import uuid
from pathlib import Path

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
import logging

# El pipeline entero registra con `logger.info`, pero nadie configuraba el
# logging: el root se queda en WARNING y esos mensajes se descartan en silencio.
# Resultado, el nodo mas caro —el filtro de humo por IA, seis minutos de los
# ocho que dura un analisis— era el unico del que no se veia una sola linea, ni
# cuanto filtraba ni por donde iba. Solo se veian los `print` sueltos.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,          # uvicorn ya instalo los suyos; este manda
)

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

class _EstaticoSinCache(StaticFiles):
    """StaticFiles que pide revalidar siempre.

    Con carpeta por job las URLs ya son unicas, asi que esto es un cinturon
    ademas de los tirantes: si un artefacto se regenera bajo la misma ruta, el
    navegador tiene que preguntar en vez de servir su copia. Sin esta cabecera
    aplica cache heuristica y puede mostrar la imagen de un analisis anterior
    sin consultar al servidor — que es exactamente como se veia el bug.
    """

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

def _guardar_artefactos(carpeta: Path, job_id: str, nombre_video: str, ancla):
    """Extrae el frame de referencia y devuelve las rutas que la vista necesita.

    Las rutas son RELATIVAS a /temp_videos, que es como esta montado el estatico:
    asi la vista arma la URL sin saber nada del disco del servidor.
    """
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
    """Deja un derivado H.264 del clip, para que el fondo de video se vea.

    EL PROBLEMA. El clip que llega del recorte viene de OpenCV con el fourcc
    `mp4v`, o sea MPEG-4 parte 2. La extension dice .mp4 —el envase es correcto—
    pero NINGUN navegador decodifica ese codec: el <video> falla con
    MEDIA_ERR_SRC_NOT_SUPPORTED (error 4) y el fondo queda en negro. Medido sobre
    un job real: `codec_name=mpeg4, codec_tag=mp4v, Simple Profile`.

    Se veia solo en la app: las vistas sobre casos congelados funcionaban porque
    `caso_desde_job.py` ya generaba su propio derivado H.264. O sea, el unico que
    NO tenia el video reproducible era el cliente.

    POR QUE UN DERIVADO Y NO CONVERTIR EL ORIGINAL. El pipeline consume el clip
    tal cual y su cache depende del archivo (ruta + tamaño + fecha): reescribirlo
    invalidaria la cache de todos los nodos y cambiaria la entrada del detector
    por una razon que no tiene nada que ver con la deteccion. El derivado es solo
    para mirar.

    CUANDO. En un hilo aparte: son ~26 s sobre un clip 4K de 92 MB, y el POST que
    dispara el analisis tiene que responder al toque con el job_id. El pipeline
    tarda minutos, asi que el derivado siempre esta listo mucho antes de que
    alguien abra la vista.

    Devuelve el nombre del archivo derivado, o None si no hay nada que hacer
    (el clip ya era H.264) o si no se puede (sin ffmpeg).
    """
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

    # 2. Una CARPETA POR ANALISIS, y el video adentro.
    #
    # Antes todo iba junto en temp_videos/ con nombres fijos, y eso rompia dos
    # cosas a la vez. El nodo que extrae eventos escribe su mascara en
    # `video_path.parent / "mascara_cambios.png"`, asi que HABIA UNA SOLA
    # mascara para toda la aplicacion: cada analisis pisaba la del anterior, y
    # abrir un job viejo mostraba la mascara del ultimo corrido —sin error, solo
    # una imagen que no corresponde—. Ademas el wizard sube todos los videos con
    # el mismo nombre (`video`), asi que el segundo analisis se llevaba por
    # delante el archivo del primero.
    #
    # Con una carpeta por job la mascara sigue al video sin tocar el nodo, y de
    # paso cada artefacto tiene una URL distinta: el navegador ya no puede
    # servir la imagen cacheada de otro analisis, que era como se veia el bug.
    job_id = str(uuid.uuid4())
    carpeta = Path(TEMP_VIDEOS) / job_id
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_video = Path(video.filename or "video").name or "video"
    # El wizard sube el recorte con el nombre `video`, SIN extension, y el
    # estatico adivina el tipo por la extension: sin ella lo sirve como
    # text/plain y el <video> del navegador no lo reproduce (el fondo de video
    # queda negro sin decir por que). Se le pone .mp4 si no trae ninguna.
    if not Path(nombre_video).suffix:
        nombre_video += ".mp4"
    video_path = str(carpeta / nombre_video)
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

    # El frame de referencia en color, para que la vista pueda poner las
    # trayectorias sobre el TERRENO y no solo sobre la mascara de cambios.
    #
    # Se toma unos frames ANTES de la primera detonacion: ahi el terreno todavia
    # esta limpio. El nodo BackgroundExtractor que ya existia sacaba el penultimo
    # frame —o sea el cuadro mas tapado de polvo de todo el clip— y ademas no
    # esta en la cadena del pipeline, asi que en la practica esta imagen nunca se
    # generaba y el fondo "Frame pre-tronadura" salia vacio en la app.
    #
    # Va en JPG y no en PNG a proposito: a 4K son ~1 MB contra ~12 MB, y esto se
    # carga por red cada vez que alguien abre la vista.
    entrada["artefactos"] = _guardar_artefactos(
        carpeta, job_id, nombre_video,
        entrada.get("recorte", {}).get("ancla_frames"))

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
        new_job = Job(id=job_id, status="Iniciando...", progress=0, entrada=entrada)
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
        
@app.get("/api/jobs")
def list_jobs(limite: int = 50):
    """Los analisis guardados, del mas nuevo al mas viejo.

    Sin esto un job solo se puede abrir si alguien anoto su id: el frontend lo
    guarda en la memoria del navegador y no lo muestra en ninguna pantalla, asi
    que al perderlo el analisis quedaba inalcanzable aunque el core lo tuviera
    entero. Es lo que necesita cualquiera que quiera retomar un analisis o
    iterar sobre uno anterior.

    NO devuelve `json_data` ni `entrada` completos: la lista se pide para
    elegir, y esos dos campos pesan del orden de un mega por job. El conteo de
    trayectorias se hace en SQL (`json_each`) para no traerlos.
    """
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