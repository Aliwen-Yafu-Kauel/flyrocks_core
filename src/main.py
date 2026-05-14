import os
import json
import shutil
import asyncio
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, WebSocket, WebSocketDisconnect, status, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# Infraestructura y Configuración
from core.config import Config
from core.database import Job, engine, Session, SQLModel
# El service ahora debe estar preparado para recibir el csv_path
from service import run_tracking_pipeline
from core.report import generar_pdf_job 

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicialización de base de datos al arrancar
    SQLModel.metadata.create_all(engine)
    yield

app = FastAPI(title="Flyrocks Tracker API - Orquestador", lifespan=lifespan)

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LÓGICA DE TAREA DE FONDO ---

def background_report_task(job_id: str, csv_path: str, json_path: str, radio_equipos: float = 250.0):
    """
    Tarea para procesar únicamente el reporte PDF.
    """
    try:
        Job.update_status(job_id, engine, status="Generando Reporte", is_running=True)
        
        pdf_filename = f"reporte_{job_id}.pdf"

        Job.update_status(job_id, engine, status="Generando Reporte PDF...", is_running=True)
        
        generar_pdf_job(csv_path=csv_path, json_path=json_path, output_pdf=pdf_filename, radio_equipos=radio_equipos)

        # Actualizar DB al terminar
        Job.update_status(
            job_id, 
            engine, 
            status="Reporte Finalizado", 
            report_file_path=f"reporte_{job_id}.pdf", 
            is_running=False  # Esto cerrará el WS en el cliente
        )
        
    except Exception as e:
        print(f"Error generando reporte: {e}")
        Job.update_status(job_id, engine, status=f"Error Reporte: {str(e)}", is_running=False)
    
    finally:
        # Limpieza de archivos de entrada
        for p in [csv_path, json_path]:
            if p and os.path.exists(p):
                os.remove(p)

def background_tracking_task(config: Config, job_id: str):
    """
    Ejecuta el pipeline completo. 
    Usa el Video para el tracking y el CSV para la correlación espacial en el reporte.
    """
    def progress_callback(curr, tot, stat, res=None):
        running = stat not in ["Completado", "Error"]
        
        # El PDF se genera al final del proceso usando el CSV
        pdf_path = f"reporte_{job_id}.pdf" if stat == "Completado" else None
        
        Job.update_status(
            job_id, 
            engine, 
            current_frame=curr, 
            total_frames=tot, 
            status=stat, 
            result_file_path=res,       # Path del JSON generado
            report_file_path=pdf_path,   # Path del PDF generado
            is_running=running
        )
        
    try:
        # IMPORTANTE: Asegúrate que run_tracking_pipeline use 
        # config.detonation_csv_path para llamar a generar_pdf_job
        run_tracking_pipeline(config=config, job_id=job_id, progress_callback=progress_callback)
        
    except Exception as e:
        print(f"Error en el pipeline: {e}")
        Job.update_status(job_id, engine, status=f"Error Crítico: {str(e)}", is_running=False)
    finally:
        # LIMPIEZA: Solo borramos los archivos una vez que el reporte PDF ya se creó.
        # El video se borra siempre para liberar espacio.
        # El CSV se borra porque ya fue procesado e integrado en los gráficos del PDF.
        files_to_clean = [config.VIDEO_PATH, getattr(config, 'detonation_csv_path', None)]
        for file_path in files_to_clean:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as ex:
                    print(f"Error al eliminar temporal {file_path}: {ex}")

# --- ENDPOINTS API ---

@app.post("/api/analyze", status_code=status.HTTP_202_ACCEPTED)
async def upload_and_analyze(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    # detonation_sequence: UploadFile = File(...), # Archivo CSV con pozos X,Y
    origin_zone: str = Form(...),
    expected_projection_zone: str = Form(...),
    h_matrix: str = Form(...)
):
    # 1. Registro en DB
    new_job = Job()
    with Session(engine) as session:
        session.add(new_job)
        session.commit()
        session.refresh(new_job)
    
    job_id = new_job.id
    
    # Rutas temporales
    video_path = f"temp_vid_{job_id}_{video.filename}"
    # csv_path = f"temp_csv_{job_id}_{detonation_sequence.filename}"

    try:
        # 2. Guardar Video
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)
            
        # # 3. Guardar CSV de Detonación
        # with open(csv_path, "wb") as buffer:
        #     shutil.copyfileobj(detonation_sequence.file, buffer)

        # 4. Crear Config (Asegúrate de haber actualizado core/config.py con este argumento)
        config = Config(
            video_path=video_path,
            # detonation_csv_path=csv_path, 
            origin_zone=json.loads(origin_zone),
            projection_zone=json.loads(expected_projection_zone),
            h_matrix=json.loads(h_matrix)
        )
        
        # 5. Ejecutar proceso
        background_tasks.add_task(background_tracking_task, config, job_id)
        
        return {"job_id": job_id, "message": "Archivos recibidos. Procesando con integración de malla de pozos."}
        
    except Exception as e:
        # Limpieza preventiva
        for p in [video_path]:
            if os.path.exists(p): os.remove(p)
        Job.update_status(job_id, engine, status=f"Error de carga: {str(e)}", is_running=False)
        raise HTTPException(status_code=500, detail=f"Error al iniciar el análisis: {str(e)}")
    
@app.post("/api/generate_report", status_code=status.HTTP_202_ACCEPTED)
async def generate_report_endpoint(
    background_tasks: BackgroundTasks,
    csv_file: UploadFile = File(...),
    json_data: UploadFile = File(...),
    radio_equipos: float = Form(250.0),
    job_id: str = Form(None)  # Puede ser un ID existente o creamos uno nuevo
):
    # 1. Si no viene job_id, creamos un registro nuevo en la DB
    if not job_id:
        new_job = Job()
        with Session(engine) as session:
            session.add(new_job)
            session.commit()
            session.refresh(new_job)
        job_id = new_job.id

    # Rutas temporales
    csv_path = f"report_input_{job_id}_{csv_file.filename}"
    json_path = f"report_input_{job_id}_{json_data.filename}"

    try:
        # 2. Guardar archivos recibidos
        with open(csv_path, "wb") as buffer:
            shutil.copyfileobj(csv_file.file, buffer)
        
        with open(json_path, "wb") as buffer:
            shutil.copyfileobj(json_data.file, buffer)

        # 3. Lanzar tarea pesada en segundo plano
        background_tasks.add_task(
            background_report_task, 
            job_id, 
            csv_path, 
            json_path,
            radio_equipos
        )

        return {
            "job_id": job_id, 
            "message": "Generando reporte PDF desde archivos proporcionados."
        }

    except Exception as e:
        for p in [csv_path, json_path]:
            if os.path.exists(p): os.remove(p)
        raise HTTPException(status_code=500, detail=f"Error al iniciar reporte: {str(e)}")

@app.get("/api/results/{job_id}")
async def download_results(job_id: str):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ID de trabajo no encontrado.")
        
        if job.is_running:
            raise HTTPException(status_code=400, detail="El análisis sigue en curso.")
            
        if not job.result_file_path or not os.path.exists(job.result_file_path):
            raise HTTPException(status_code=404, detail="Archivo de resultados no disponible.")
            
        return FileResponse(
            path=job.result_file_path, 
            filename=f"flyrocks_{job_id}.json"
        )


@app.get("/api/report/{job_id}")
async def download_report(job_id: str):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job or not job.report_file_path or not os.path.exists(job.report_file_path):
            raise HTTPException(status_code=404, detail="El reporte PDF con análisis de pozos aún no está listo.")
            
        return FileResponse(
            path=job.report_file_path, 
            filename=f"Reporte_Tecnico_{job_id}.pdf",
            media_type='application/pdf'
        )

# --- WEBSOCKET DE PROGRESO ---

@app.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            with Session(engine) as session:
                job = session.get(Job, job_id)
                if not job: break

                percentage = round((job.current_frame / job.total_frames * 100), 2) if job.total_frames > 0 else 0
                
                await websocket.send_json({
                    "status": job.status,
                    "percentage": percentage,
                    "is_running": job.is_running,
                    "has_report": job.report_file_path is not None
                })
                
                if not job.is_running: break
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)