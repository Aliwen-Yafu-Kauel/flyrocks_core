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
import numpy as np
from pydantic import BaseModel
from typing import List, Dict, Any
from utils.nodes.velocity_analysis import HighVelocityFilterNode
from utils.nodes.trajectory_categorization import TrajectoryCategorizationNode
from utils.nodes.trajectory_analysis import GridSearchNode, KalmanTrackerNode, TrajectoryCleanerNode
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
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ENDPOINT PARA DISPARAR EL ANÁLISIS ---
@app.post("/api/analyze")
async def start_analysis(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    origin_zone: str = Form(...),
    expected_projection_zone: str = Form(...),
    h_matrix: str = Form(...)
):
    try:
        origin_zone_parsed = json.loads(origin_zone)
        expected_zone_parsed = json.loads(expected_projection_zone)
        h_matrix_parsed = json.loads(h_matrix)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Los parámetros de zonas o matriz deben ser JSON válidos.")

    video_path = f"temp_videos/{video.filename}"
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(video.file, buffer)

    with Session(engine) as session:
        new_job = Job(status="Iniciando...", progress=0)
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
        output_filename="background.jpg"  
    )
    
    return {"job_id": new_job.id, "mensaje": "Análisis encolado en segundo plano"}

# --- WEBSOCKET PARA NOTIFICAR EL AVANCE ---
@app.websocket("/ws/progress/{job_id}")
async def websocket_job_status(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            job_data = None
            
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
                print(f"⏳ Base de datos ocupada. Reintentando...")
                await asyncio.sleep(1)
                continue 

            if not job_data:
                await websocket.send_json({"error": "Job no encontrado"})
                break
            
            await websocket.send_json(job_data)

            if not job_data["is_running"]:
                break
                
            await asyncio.sleep(1)
            
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
        job = session.get(Job, job_id)
        
        if not job:
            raise HTTPException(status_code=404, detail="Análisis no encontrado")
        
        return job

# --- ESQUEMA PARA LA UNIFICACIÓN ---
class UnificacionRequest(BaseModel):
    trayectorias: list
    h_matrix: list
    expected_projection_zone: list
    video_filename: str
    base_patience: int  # <-- Nuevo parámetro heredado

# --- ENDPOINT DE UNIFICACIÓN ESTRICTO ---
@app.post("/api/unificar_trayectorias")
def unificar_trayectorias(request: UnificacionRequest):
    print("--- INICIANDO UNIFICACIÓN MULTI-ESCENARIO ---")
    
    escenarios_resultados = {}
    multiplicadores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]  # <-- Escenarios de multiplicación de paciencia

    for mult in multiplicadores:
        # 1. Construir las detecciones DESDE CERO en cada iteración
        frames_unique = set()
        detections_by_frame = {}
        for tray in request.trayectorias:
            puntos = tray.get("puntos", tray.get("puntos_px", tray.get("points", [])))
            frames = tray.get("frames", tray.get("frame_ids", []))
            
            for pt, f in zip(puntos, frames):
                frames_unique.add(f)
                if f not in detections_by_frame:
                    detections_by_frame[f] = []
                
                # 🚀 CORRECCIÓN A: Pasamos estrictamente [x, y], sin el 1.0 al final
                detections_by_frame[f].append([pt[0], pt[1]])
                
        frames_unique = sorted(list(frames_unique))
        
        # 🚀 CORRECCIÓN B: Convertimos las listas de Python a tensores de Numpy
        for f in detections_by_frame:
            detections_by_frame[f] = np.array(detections_by_frame[f])
        
        # 2. Definir la paciencia y distancia exactas para ESTE escenario
        paciencia_calculada = int(request.base_patience * mult)
        distancia_calculada = float(30.0 * np.log(2+mult)) 
        
        # 3. Armar el contexto sin el GridSearchNode
        context = {
            "unique_frames": frames_unique, 
            "detections_by_frame": detections_by_frame,
            "velocity_threshold": 0.0,
            "h_matrix": request.h_matrix,
            "expected_projection_zone": request.expected_projection_zone,
            "video_path": f"temp_videos/{request.video_filename}",
            "optimal_patience": paciencia_calculada,
            "max_dist": distancia_calculada # 🚀 NUEVO: Inyectamos el radio expandido
        }
        
        # 4. Ejecutar la tubería estricta
        context = KalmanTrackerNode(name=f"Kalman_{mult}").run(context)
        context = TrajectoryCleanerNode(name=f"Cleaner_{mult}").run(context)
        context = HighVelocityFilterNode(name=f"Filter_{mult}").run(context)
        context = TrajectoryCategorizationNode(name=f"Categorizer_{mult}", output_filename=None).run(context)
        
        # 5. Extraer y guardar las trayectorias de este escenario
        # 🚀 CORRECCIÓN: Extraemos desde json_resultados, que es donde el Categorizador deja la lista final
        resultado = context.get("json_resultados", {}).get("trayectorias", [])
        
        escenarios_resultados[str(mult)] = resultado
        
        print(f"✅ Escenario {mult}x (Paciencia: {paciencia_calculada}) -> {len(resultado)} trayectorias resultantes.")

    # Retornamos el diccionario completo con todos los escenarios pre-calculados
    return {"escenarios": escenarios_resultados}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)