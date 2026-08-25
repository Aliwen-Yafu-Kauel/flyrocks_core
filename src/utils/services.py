import os   
import json
import logging
from pathlib import Path
from typing import Any, Dict

from sqlmodel import Session
from utils.database import Job, engine, DATA_DIR

TEMP_VIDEOS = os.path.join(DATA_DIR, "temp_videos")
MODELO_ONNX = os.getenv("MODELO_ONNX", "modelos/detovision_model_v18.onnx")
IA_ACTIVA = os.getenv("IA_ACTIVA", "1").lower() not in ("0", "false", "no")

from utils.nodes.base import ejecutar
from utils.nodes.event_extractor import EventExtractorNode
from utils.nodes.ai_smoke_filter import AISmokeFilterNode 
from utils.nodes.percentile_preview import PercentilePreviewNode  

from utils.nodes.trajectory_analysis import (
    EnergyPercentileFilterNode, DBSCANClusteringNode, 
    GridSearchNode, KalmanTrackerNode, TrajectoryCleanerNode
)

from utils.nodes.velocity_analysis import (
    HighVelocityFilterNode, TrajectoryVelocityNode, GaussianThresholdNode
)

from utils.nodes.trajectory_categorization import TrajectoryCategorizationNode
from utils.nodes.trajectory_filters import (
    TortuosityCalculationNode, 
    OriginAreaExpansionNode,
    TrajectorySmoothnessNode
) 

logger = logging.getLogger(__name__)

TOTAL_CORES = os.cpu_count() or 4
CORES = max(1, TOTAL_CORES - 2)

def run_pipeline_task(
    job_id: str, 
    video_path: str, 
    origin_zone: list,                 
    expected_projection_zone: list,    
    h_matrix: list,
    percentile: float,
    sigma: float,
    esp: float,                
    output_filename: str = "results.json"
):
    logger.info(f"Iniciando procesamiento de pipeline para job {job_id} - {video_path}")
    
    # 1. Determinar si estamos en Fase 1 (Nuevo) o Fase 2 (Reanudando desde Frontend)
    is_resume = False
    with Session(engine) as session:
        job = session.get(Job, job_id)
        # Este estado exacto es el que seteamos en POST /api/resume/{job_id}
        if job and job.status == "Reanudando pipeline con nuevo percentil...":
            is_resume = True

    try:
        if not is_resume:
            Job.update_status(job_id, engine, status="Iniciando extracción...", progress=5)
        
        # --- Instanciar todos los Nodos ---
        extractor = EventExtractorNode(name="1_VideoExtractor", noise_threshold=8)
        ai_smoke = AISmokeFilterNode(name="1.5_AISmokeFilter", onnx_path=MODELO_ONNX)
        percentile_preview = PercentilePreviewNode(name="1.8_PercentilePreview") 
        
        energy_filter = EnergyPercentileFilterNode(name="2_EnergyFilter", percentile=percentile)
        clustering = DBSCANClusteringNode(name="3_SpatialClustering", eps=esp)
        grid_search = GridSearchNode(name="4_GridSearchOptimizer", cores=CORES)
        tracker = KalmanTrackerNode(name="5_KalmanTracker")
        cleaner = TrajectoryCleanerNode(name="6_TrajectoryCleaner")
        
        if IA_ACTIVA:
            rock_filter = HighVelocityFilterNode(name="9_HighVelocityFilter", manual_threshold=0.0)
            pasos_velocidad = []
        else:
            velocity_calc = TrajectoryVelocityNode(name="7_VelocityCalculator")
            threshold_calc = GaussianThresholdNode(name="8_GaussianThreshold", sigma_multiplier=sigma)
            rock_filter = HighVelocityFilterNode(name="9_HighVelocityFilter")
            pasos_velocidad = [
                (velocity_calc, "Calculando cinemática", 60),
                (threshold_calc, "Calculando umbral gaussiano", 65),
            ]
        
        categorizer = TrajectoryCategorizationNode(name="11_TrajectoryCategorizer")
        tortuosity_calc = TortuosityCalculationNode(name="12_TortuosityCalculation")
        origin_area_expansion = OriginAreaExpansionNode(name="13_OriginAreaExpansion")
        smoothness_calc = TrajectorySmoothnessNode(name="14_TrajectorySmoothness")

        # --- Dividir el Pipeline en Fases ---
        fase_1 = [
            (extractor, "Extrayendo eventos del video", 10),
            *([(ai_smoke, "Filtrando humo con IA (ONNX)", 15)] if IA_ACTIVA else []),
            (percentile_preview, "Generando vista previa de corte", 18)
        ]
        
        fase_2 = [
            (energy_filter, "Filtrando energía (Percentil)", 20),
            (clustering, "Ejecutando clustering DBSCAN", 35),
            (grid_search, "Optimizando Grid Search", 45),
            (tracker, "Rastreando partículas (Kalman)", 55),
            (cleaner, "Limpiando trayectorias inválidas", 60),
            *pasos_velocidad,
            (rock_filter, "Formateando trayectorias trackeadas" if IA_ACTIVA else "Filtrando por velocidad", 70),
            (categorizer, "Categorizando trayectorias", 80),
            (tortuosity_calc, "Calculando tortuosidad", 85),
            (origin_area_expansion, "Calculando expansión de área de origen", 90),
            (smoothness_calc, "Calculando suavidad de trayectorias", 95)
        ]

        # Si es reanudación, cargamos la cadena entera (La caché saltará la fase 1 al instante)
        # Si es la primera vez, cargamos solo la Fase 1.
        pipeline_steps = fase_1 + fase_2 if is_resume else fase_1

        context: Dict[str, Any] = {
            "job_id": job_id,
            "job_dir": os.path.dirname(video_path),
            "video_path": video_path,
            "origin_zone": origin_zone,
            "expected_projection_zone": expected_projection_zone,
            "h_matrix": h_matrix
        }

        def avisar(i, total, node):
            _, status_msg, progress_val = pipeline_steps[i]
            Job.update_status(job_id, engine, status=status_msg, progress=progress_val)

        try:
            context = ejecutar([n for n, _, _ in pipeline_steps], context, progreso=avisar)
        except Exception as e:
            logger.error(f"Error crítico en el pipeline: {str(e)}")
            raise e

        if "error" in context:
            raise Exception(f"El pipeline reportó un error: {context['error']}")

        # --- EL CORTE REAL ---
        # Si acabamos de correr la Fase 1, hacemos un return anticipado para detener el proceso.
        if not is_resume:
            logger.info(f"Fase 1 completada. Hilo pausado exitosamente esperando confirmación del usuario (Job: {job_id}).")
            return

        # Si estábamos en Fase 2, el proceso llegó hasta el final: guardamos el resultado
        results = context.get('json_resultados', {})
        if output_filename and results:
            output_path = Path(TEMP_VIDEOS) / output_filename
            output_path.parent.mkdir(parents=True, exist_ok=True) 
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=4, ensure_ascii=False)

        logger.info("Pipeline completado con éxito.")
        Job.update_status(
            job_id, 
            engine, 
            is_running=False, 
            status="Completado", 
            progress=100,
            result_file_path=output_filename,
            json_data=results
        )

    except Exception as e:
        logger.error(f"Error fatal en el pipeline para job {job_id}: {str(e)}")
        Job.update_status(
            job_id, 
            engine, 
            is_running=False, 
            status="Error en el procesamiento", 
            error_message=str(e)
        )