import os   
import json
import logging
from pathlib import Path
from typing import Any, Dict

from utils.database import Job, engine, DATA_DIR

# Videos subidos y JSON de resultados: bajo DATA_DIR para que sobrevivan al
# `docker compose down` que hace el script del cliente al iniciar y al detener.
TEMP_VIDEOS = os.path.join(DATA_DIR, "temp_videos")

# Los pesos del filtro de humo por IA. Se puede apuntar a otro archivo sin
# reconstruir la imagen (util para probar una version del modelo): la ruta por
# defecto es relativa a /app, que es el directorio de trabajo del contenedor.
MODELO_ONNX = os.getenv("MODELO_ONNX", "modelos/detovision_model_v18.onnx")

# El filtro de humo por IA se puede APAGAR con IA_ACTIVA=0.
#
# Sirve para tres cosas concretas: iterar en las vistas y las herramientas sin
# esperar los ~6 minutos que tarda la inferencia; trabajar en un equipo que no
# llega a los 10 GB de RAM para Docker; y tener un plan B si el modelo falla o
# no esta. Con la IA apagada el pipeline es el de siempre, con su filtro de
# velocidad, que es el que producia ~726 trayectorias en el clip de referencia
# contra las ~3742 del camino con IA.
IA_ACTIVA = os.getenv("IA_ACTIVA", "1").lower() not in ("0", "false", "no")

from utils.nodes.base import ejecutar
from utils.nodes.event_extractor import EventExtractorNode

# NUEVO: Importamos el nodo de IA para el filtro de humo
from utils.nodes.ai_smoke_filter import AISmokeFilterNode 

from utils.nodes.trajectory_analysis import (
    EnergyPercentileFilterNode, DBSCANClusteringNode, 
    GridSearchNode, KalmanTrackerNode, TrajectoryCleanerNode
)

# Con la IA activa el filtro de velocidad queda en bypass y estos dos nodos no
# se usan; con IA_ACTIVA=0 vuelven, porque son los que calculan el umbral
# dinamico del camino clasico. Se importan siempre: elegir la cadena es una
# decision de tiempo de ejecucion, no de importacion.
from utils.nodes.velocity_analysis import (
    HighVelocityFilterNode, TrajectoryVelocityNode, GaussianThresholdNode
)

from utils.nodes.trajectory_smoothness import TrajectorySmoothnessNode
from utils.nodes.image_renderer import BackgroundExtractorNode
from utils.nodes.trajectory_categorization import TrajectoryCategorizationNode
from utils.nodes.trajectory_filters import (
    TortuosityCalculationNode, OriginAreaExpansionNode,
    TrajectorySmoothnessNode
) 
# from utils.nodes.visualization import VideoRendererNode

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
    """
    Servicio de ejecución en segundo plano.
    Actualiza el estado en la base de datos y guarda el JSON resultante al finalizar.
    """
    logger.info(f"Iniciando procesamiento de pipeline para job {job_id} - {video_path}")

    try:
        Job.update_status(job_id, engine, status="Iniciando extracción...", progress=5)
        
        # --- Instanciamos los nodos ---
        extractor = EventExtractorNode(name="1_VideoExtractor", noise_threshold=8)
        
        # NUEVO: Instanciamos el filtro de humo con IA
        ai_smoke = AISmokeFilterNode(name="1.5_AISmokeFilter",
                                     onnx_path=MODELO_ONNX)
        
        energy_filter = EnergyPercentileFilterNode(name="2_EnergyFilter", percentile=percentile)
        clustering = DBSCANClusteringNode(name="3_SpatialClustering", eps=esp)
        grid_search = GridSearchNode(name="4_GridSearchOptimizer", cores=CORES)
        tracker = KalmanTrackerNode(name="5_KalmanTracker")
        cleaner = TrajectoryCleanerNode(name="6_TrajectoryCleaner")
        
        # Con IA: el filtro de velocidad pasa TODO (umbral 0.0) porque la
        # limpieza ya la hizo el modelo, y el nodo queda solo para dar formato.
        # Sin IA: vuelve el camino clasico, con la cinematica y el umbral
        # gaussiano que alimentan al filtro.
        if IA_ACTIVA:
            rock_filter = HighVelocityFilterNode(name="9_HighVelocityFilter",
                                                 manual_threshold=0.0)
            pasos_velocidad = []
        else:
            velocity_calc = TrajectoryVelocityNode(name="7_VelocityCalculator")
            threshold_calc = GaussianThresholdNode(name="8_GaussianThreshold",
                                                   sigma_multiplier=sigma)
            rock_filter = HighVelocityFilterNode(name="9_HighVelocityFilter")
            pasos_velocidad = [
                (velocity_calc, "Calculando cinemática", 60),
                (threshold_calc, "Calculando umbral gaussiano", 65),
            ]
        
        categorizer = TrajectoryCategorizationNode(name="11_TrajectoryCategorizer")
        tortuosity_calc = TortuosityCalculationNode(name="12_TortuosityCalculation")
        origin_area_expansion = OriginAreaExpansionNode(name="13_OriginAreaExpansion")
        smoothness_calc = TrajectorySmoothnessNode(name="14_TrajectorySmoothness")
        # render = VideoRendererNode(name="14_VideoRenderer", output_filename=output_filename)

        # La cadena cambia segun el interruptor: con IA entra el filtro de humo y
        # el de velocidad queda en bypass; sin ella, el camino clasico.
        pipeline_steps = [
            (extractor, "Extrayendo eventos del video", 10),
            *([(ai_smoke, "Filtrando humo con IA (ONNX)", 15)] if IA_ACTIVA else []),
            (energy_filter, "Filtrando energía (Percentil)", 20),
            (clustering, "Ejecutando clustering DBSCAN", 35),
            (grid_search, "Optimizando Grid Search", 45),
            (tracker, "Rastreando partículas (Kalman)", 55),
            (cleaner, "Limpiando trayectorias inválidas", 60),
            *pasos_velocidad,
            (rock_filter, "Formateando trayectorias trackeadas" if IA_ACTIVA
                          else "Filtrando por velocidad", 70),
            (categorizer, "Categorizando trayectorias", 80),
            (tortuosity_calc, "Calculando tortuosidad", 85),
            (origin_area_expansion, "Calculando expansión de área de origen", 90),
            (smoothness_calc, "Calculando suavidad de trayectorias", 95),
            # (render, "Renderizando video final", 98)
        ]

        context: Dict[str, Any] = {
            "video_path": video_path,
            "origin_zone": origin_zone,
            "expected_projection_zone": expected_projection_zone,
            "h_matrix": h_matrix
        }

        # Ejecución secuencial, reusando lo que ya esté calculado. El avance se
        # reporta desde el callback: así los nodos que salen de caché no fingen
        # estar procesando, y la barra salta directo al primero que sí corre.
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

        # Guardado del JSON resultante
        results = context.get('json_resultados', {})
        if output_filename and results:
            output_path = Path(TEMP_VIDEOS) / output_filename
            output_path.parent.mkdir(parents=True, exist_ok=True) 
            
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=4, ensure_ascii=False)

        # Finalización
        logger.info("Pipeline completado con éxito (IA Smoke Filter activado).")
        
        Job.update_status(
            job_id, 
            engine, 
            is_running=False, 
            status=("Completado (IA Activa, Velocidad Bypasseada)" if IA_ACTIVA
                    else "Completado (sin IA)"), 
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