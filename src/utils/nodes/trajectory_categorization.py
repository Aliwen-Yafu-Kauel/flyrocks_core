import cv2
import numpy as np
import logging
from typing import Any, Dict
from .base import PipelineNode

logger = logging.getLogger(__name__)

class TrajectoryCategorizationNode(PipelineNode):
    
    def __init__(self, name: str = "11_TrajectoryCategorizer", margin_px: int = 5):
        super().__init__(name)
        self.margin_px = margin_px

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"[{self.name}] Iniciando categorización de trayectorias...")
                
        # 1. Obtener datos del contexto
        trajectories = context.get("filtered_rocks_dict", {}) 
        safety_zone_raw = context.get("expected_projection_zone")
        video_path = context.get("video_path")
        h_matrix_raw = context.get("h_matrix")

        if not trajectories or not safety_zone_raw or not video_path or not h_matrix_raw:
            context["error"] = "Faltan datos en el contexto para la categorización."
            return context

        safety_polygon = np.array(safety_zone_raw, dtype=np.int32).reshape((-1, 1, 2))
        h_matrix = np.array(h_matrix_raw, dtype=np.float64).reshape(3, 3)
        
        try:
            h_inv = np.linalg.inv(h_matrix)
        except np.linalg.LinAlgError:
            context["error"] = "La matriz de homografía proporcionada no es invertible."
            return context

        cap = cv2.VideoCapture(str(video_path))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        resultados_json = {}
        min_x, max_x = self.margin_px, width - self.margin_px
        min_y, max_y = self.margin_px, height - self.margin_px

        for track_id, traj_array in trajectories.items():
            puntos_lista = traj_array[:, 1:3].astype(int).flatten().tolist()
            if not puntos_lista: continue

            # El tensor viene ordenado por (id, t) desde HighVelocityFilterNode,
            # asi que la columna 3 es el frame de cada punto y va en ascendente.
            # Se exporta junto a los puntos porque sin el eje temporal no se
            # puede cruzar la trayectoria con los tiempos de la secuencia de
            # detonacion (causalidad y tiempo de vuelo).
            frames_lista = traj_array[:, 3].astype(int).tolist()

            first_x, first_y = float(traj_array[0, 1]), float(traj_array[0, 2])
            last_x, last_y = float(traj_array[-1, 1]), float(traj_array[-1, 2])
            
            pts_px = np.array([[first_x, first_y], [last_x, last_y]], dtype=np.float32).reshape(-1, 1, 2)
            pts_metros = cv2.perspectiveTransform(pts_px, h_inv)
            
            real_first_x, real_first_y = pts_metros[0][0]
            real_last_x, real_last_y = pts_metros[1][0]
            distancia_m = float(np.hypot(real_last_x - real_first_x, real_last_y - real_first_y))

            if last_x <= min_x or last_x >= max_x or last_y <= min_y or last_y >= max_y:
                categoria = "Fuera de vista"
            else:
                distancia_poly = cv2.pointPolygonTest(safety_polygon, (last_x, last_y), measureDist=False)
                categoria = "Proyección" if distancia_poly >= 0 else "Proyección peligrosa"

            resultados_json[str(track_id)] = {
                "clasificacion": categoria,
                "distancia_m": round(distancia_m, 2),
                "puntos": puntos_lista,
                "frames": frames_lista
            }

        context["json_resultados"] = resultados_json
        return context