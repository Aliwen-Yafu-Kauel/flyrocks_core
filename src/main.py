import os
import json
import shutil
import asyncio
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, File, UploadFile, Form, HTTPException
from sqlmodel import SQLModel, Session
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from utils.database import engine, Job
from utils.services import run_pipeline_task

# Aseguramos que exista una carpeta para guardar los videos que suba el usuario
os.makedirs("temp_videos", exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Inicializando recursos de la aplicación...")
    SQLModel.metadata.create_all(engine)
    yield 
    print("Apagando la aplicación y liberando recursos...")
    engine.dispose()

app = FastAPI(title="API de Análisis Flyrocks", lifespan=lifespan)

app.mount("/temp_videos", StaticFiles(directory="temp_videos"), name="temp_videos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En desarrollo permitimos todo. En prod, pones la URL de tu front
    allow_credentials=True,
    allow_methods=["*"],  # Permite POST, GET, OPTIONS, etc.
    allow_headers=["*"],
)

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
    esp: float = Form(..., ge=1.0, le=7.0)
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
    video_path = f"temp_videos/{video.filename}"
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    # 3. Creamos el registro en la DB
    with Session(engine) as session:
        new_job = Job(status="Iniciando...", progress=0)
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