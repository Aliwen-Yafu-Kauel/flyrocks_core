import logging
from typing import Any, Dict

from utils.database import Job, engine
from utils.nodes.event_extractor import EventExtractorNode
from utils.nodes.trajectory_filters import ZigzagFilterNode
from utils.nodes.trajectory_analysis import (
    EnergyPercentileFilterNode, DBSCANClusteringNode, 
    GridSearchNode, KalmanTrackerNode, TrajectoryCleanerNode
)
from utils.nodes.velocity_analysis import (
    TrajectoryVelocityNode, GaussianThresholdNode,
    HighVelocityFilterNode  
)  
from utils.nodes.trajectory_categorization import TrajectoryCategorizationNode
from utils.nodes.trajectory_filters import ZigzagFilterNode, OriginZoneFilterNode
from utils.nodes.visualization import VideoRendererNode

logger = logging.getLogger(__name__)

def run_pipeline_task(
    job_id: str, 
    video_path: str, 
    origin_zone: list,                 
    expected_projection_zone: list,    
    h_matrix: list,                    
    output_filename: str = "" # Guardamos en temp_videos
):
    """
    Servicio de ejecución en segundo plano con DEBUG EXTREMO.
    Actualiza la DB paso a paso e imprime en consola cada movimiento.
    """
    print(f"\n{'='*60}")
    print(f"🟢 INICIANDO JOB ID: {job_id}")
    print(f"📂 Video a procesar: {video_path}")
    print(f"{'='*60}\n")

    try:
        Job.update_status(job_id, engine, status="Iniciando extracción...", progress=5)
        
        # --- Instanciamos los nodos ---
        extractor = EventExtractorNode(name="1_VideoExtractor", noise_threshold=8)
        energy_filter = EnergyPercentileFilterNode(name="2_EnergyFilter", percentile=96.0)
        clustering = DBSCANClusteringNode(name="3_SpatialClustering", eps=5.0)
        grid_search = GridSearchNode(name="4_GridSearchOptimizer", cores=4)
        tracker = KalmanTrackerNode(name="5_KalmanTracker")
        cleaner = TrajectoryCleanerNode(name="6_TrajectoryCleaner")
        velocity_calc = TrajectoryVelocityNode(name="7_VelocityCalculator")
        threshold_calc = GaussianThresholdNode(name="8_GaussianThreshold", sigma_multiplier=0.5)
        rock_filter = HighVelocityFilterNode(name="9_HighVelocityFilter")
        zizag_filter = ZigzagFilterNode(name="10_ZigzagFilter", max_tortuosity=1.25)
        origin_filter = OriginZoneFilterNode(name="10_OriginZoneFilter")
        categorizer = TrajectoryCategorizationNode(name="11_TrajectoryCategorizer")
        #render = VideoRendererNode(name="11_VideoRenderer", output_filename=output_filename)

        # Mapeamos los nodos con su mensaje de estado y % de progreso esperado al finalizar
        pipeline_steps = [
            (extractor, "Extrayendo eventos del video", 10),
            (energy_filter, "Filtrando energía (Percentil)", 15),
            (clustering, "Ejecutando clustering DBSCAN", 35),
            (grid_search, "Optimizando Grid Search", 45),
            (tracker, "Rastreando partículas (Kalman)", 55),
            (cleaner, "Limpiando trayectorias inválidas", 60),
            (velocity_calc, "Calculando cinemática", 65),
            (threshold_calc, "Calculando umbral gaussiano", 70),
            (rock_filter, "Filtrando por velocidad", 75),
            (zizag_filter, "Filtrando trayectorias invalidas", 80),
            (origin_filter, "Filtrando trayectorias fuera de zona de origen", 85),
            (categorizer, "Categorizando trayectorias", 90),

            #(render, "Renderizando video final", 98)
        ]

        # Inyectamos todos los datos que vienen del frontend
        context: Dict[str, Any] = {
            "video_path": video_path,
            "origin_zone": origin_zone,
            "expected_projection_zone": expected_projection_zone,
            "h_matrix": h_matrix
        }

        # Ejecución secuencial con DEBUG
        for node, status_msg, progress_val in pipeline_steps:
            print(f"\n{'='*50}")
            print(f"🚀 INICIANDO NODO: {node.name}")
            print(f"📝 Mensaje al front: {status_msg} ({progress_val}%)")
            
            # 1. Actualizamos BD antes de ejecutar el nodo
            Job.update_status(job_id, engine, status=status_msg, progress=progress_val)
            
            # 2. Ejecutamos el nodo
            try:
                context = node.run(context)
            except Exception as e:
                print(f"❌ ERROR CRÍTICO DENTRO DEL NODO {node.name}: {str(e)}")
                raise e # Lanzamos el error para que el except principal lo atrape
            
            # 3. Verificamos si el nodo reportó un error en el diccionario context
            if "error" in context:
                print(f"⚠️ EL NODO {node.name} DEVOLVIÓ UN ERROR EN EL CONTEXTO: {context['error']}")
                raise Exception(context["error"])
                
            # 4. Avisamos que el nodo terminó con éxito
            print(f"✅ NODO COMPLETADO EXITOSAMENTE: {node.name}")
            print(f"{'='*50}\n")

        # Finalización exitosa
        final_velocity = context.get('velocity_threshold', 0.0)
        logger.info(f"Pipeline completado. Umbral: {final_velocity:.2f} px/f")
        results = context.get('json_resultados', {})
        print(f"🎉 PIPELINE COMPLETADO CON ÉXITO. Umbral calculado: {final_velocity:.2f}")
        
        Job.update_status(
            job_id, 
            engine, 
            is_running=False, 
            status=f"Completado (Umbral: {final_velocity:.2f} px/f)", 
            progress=100,
            result_file_path=output_filename,
            json_data=results
        )

    except Exception as e:
        print(f"\n🚨 ERROR FATAL EN EL PIPELINE: {str(e)}")
        logger.error(f"Error en job {job_id}: {str(e)}")
        Job.update_status(
            job_id, 
            engine, 
            is_running=False, 
            status="Error en el procesamiento", 
            error_message=str(e)
        )