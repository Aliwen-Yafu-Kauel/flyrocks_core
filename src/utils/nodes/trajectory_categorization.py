import cv2
import json
import numpy as np
import logging
from pathlib import Path
from typing import Any, Dict

from .base import PipelineNode

logger = logging.getLogger(__name__)

class TrajectoryCategorizationNode(PipelineNode):
    
    def __init__(self, name: str = "10_TrajectoryCategorizer", margin_px: int = 5, output_filename: str = "datos_flyrocks.json"):
        super().__init__(name)
        self.margin_px = margin_px
        self.output_filename = output_filename

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        print(f"[{self.name}] Iniciando categorización de trayectorias y cálculo de distancias...")
        
        # 1. Obtener datos del contexto
        trajectories = context.get("filtered_rocks_dict", {}) 
        safety_zone_raw = context.get("expected_projection_zone")
        video_path = context.get("video_path")
        h_matrix_raw = context.get("h_matrix")

        if not trajectories:
            context["error"] = "No se encontraron rocas filtradas (filtered_rocks_dict) para categorizar."
            return context
            
        if not safety_zone_raw or not video_path or not h_matrix_raw:
            context["error"] = "Faltan datos en el contexto (zona seguridad, video o matriz H)."
            return context

        safety_polygon = np.array(safety_zone_raw, dtype=np.int32)
        if len(safety_polygon.shape) == 2:
            safety_polygon = safety_polygon.reshape((-1, 1, 2))
            
        h_matrix = np.array(h_matrix_raw, dtype=np.float64).reshape(3, 3)
        
        try:
            h_inv = np.linalg.inv(h_matrix)
        except np.linalg.LinAlgError:
            context["error"] = "La matriz de homografía proporcionada no es invertible."
            return context

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            context["error"] = "No se pudo abrir el video para obtener dimensiones."
            return context
            
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        # 🚀 SOLUCIÓN: Separar el dict legacy del formato de lista esperado
        resultados_dict = {}
        trayectorias_list = []
        
        min_x, max_x = self.margin_px, width - self.margin_px
        min_y, max_y = self.margin_px, height - self.margin_px

        # 4. Analizar cada trayectoria
        for track_id, traj_array in trajectories.items():
            
            # 🚀 SOLUCIÓN: Quitar el .flatten() para conservar estructura [[x,y], [x,y]]
            puntos_lista = traj_array[:, 1:3].astype(int).tolist()

            if not puntos_lista:
                continue

            first_x, first_y = float(traj_array[0, 1]), float(traj_array[0, 2])
            last_x, last_y = float(traj_array[-1, 1]), float(traj_array[-1, 2])
            
            pts_px = np.array([
                [first_x, first_y], 
                [last_x, last_y]
            ], dtype=np.float32).reshape(-1, 1, 2)
            
            pts_metros = cv2.perspectiveTransform(pts_px, h_inv)
            
            real_first_x, real_first_y = pts_metros[0][0]
            real_last_x, real_last_y = pts_metros[1][0]
            
            distancia_m = float(np.hypot(real_last_x - real_first_x, real_last_y - real_first_y))

            if last_x <= min_x or last_x >= max_x or last_y <= min_y or last_y >= max_y:
                categoria = "Fuera de vista"
            else:
                distancia_poly = cv2.pointPolygonTest(safety_polygon, (last_x, last_y), measureDist=False)
                if distancia_poly >= 0:
                    categoria = "Proyección"
                else:
                    categoria = "Proyección peligrosa"

            traj_data = {
                "id": str(track_id),
                "clasificacion": categoria,
                "distancia_m": round(distancia_m, 2),
                "puntos": puntos_lista 
            }
            
            resultados_dict[str(track_id)] = traj_data
            trayectorias_list.append(traj_data)

        # 5. Guardamos el formato legacy en disco para no romper otros reportes posibles
        output_path = Path("temp_videos") / self.output_filename
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultados_dict, f, indent=4, ensure_ascii=False)
            
        print(f"[{self.name}] {len(resultados_dict)} trayectorias procesadas (categorizadas y medidas).")

        # 6. 🚀 SOLUCIÓN: Inyectar al contexto el formato JSON exacto que espera Smoothness y JS
        context["json_resultados"] = {
            "trayectorias": trayectorias_list
        }
        context["json_file_path"] = str(output_path)

        return context
        print(f"[{self.name}] Iniciando categorización de trayectorias y cálculo de distancias...")
        
        # 1. Obtener datos del contexto
        trajectories = context.get("filtered_rocks_dict", {}) 
        safety_zone_raw = context.get("expected_projection_zone")
        video_path = context.get("video_path")
        h_matrix_raw = context.get("h_matrix")

        if not trajectories:
            context["error"] = "No se encontraron rocas filtradas (filtered_rocks_dict) para categorizar."
            return context
            
        if not safety_zone_raw or not video_path or not h_matrix_raw:
            context["error"] = "Faltan datos en el contexto (zona seguridad, video o matriz H)."
            return context

        # 2. Preparar polígono y matrices
        # OpenCV exige tipo de dato int32 y forma (N, 1, 2)
        safety_polygon = np.array(safety_zone_raw, dtype=np.int32)
        if len(safety_polygon.shape) == 2:
            safety_polygon = safety_polygon.reshape((-1, 1, 2))
            
        # Matriz H (Metros -> Pixeles) a float64 para máxima precisión
        h_matrix = np.array(h_matrix_raw, dtype=np.float64).reshape(3, 3)
        
        # Calculamos la inversa H^-1 (Pixeles -> Metros)
        try:
            h_inv = np.linalg.inv(h_matrix)
        except np.linalg.LinAlgError:
            context["error"] = "La matriz de homografía proporcionada no es invertible."
            return context

        # 3. Obtener dimensiones del video
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            context["error"] = "No se pudo abrir el video para obtener dimensiones."
            return context
            
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        resultados_json = {}
        
        # Limites para "Fuera de vista"
        min_x, max_x = self.margin_px, width - self.margin_px
        min_y, max_y = self.margin_px, height - self.margin_px

        # 4. Analizar cada trayectoria
        for track_id, traj_array in trajectories.items():
            
            puntos_lista = traj_array[:, 1:3].astype(int).flatten().tolist()

            if not puntos_lista:
                continue

            # Tomamos el primer y último punto en pixeles
            first_x, first_y = float(traj_array[0, 1]), float(traj_array[0, 2])
            last_x, last_y = float(traj_array[-1, 1]), float(traj_array[-1, 2])
            
            # Preparamos los puntos para OpenCV forzando la forma (N, 1, 2)
            pts_px = np.array([
                [first_x, first_y], 
                [last_x, last_y]
            ], dtype=np.float32).reshape(-1, 1, 2)
            
            # Transformamos de Pixeles a Metros usando H^-1
            pts_metros = cv2.perspectiveTransform(pts_px, h_inv)
            
            real_first_x, real_first_y = pts_metros[0][0]
            real_last_x, real_last_y = pts_metros[1][0]
            
            # Calculamos la distancia Euclidiana en metros
            distancia_m = float(np.hypot(real_last_x - real_first_x, real_last_y - real_first_y))

            # --- CATEGORIZACIÓN ---
            if last_x <= min_x or last_x >= max_x or last_y <= min_y or last_y >= max_y:
                categoria = "Fuera de vista"
            else:
                distancia_poly = cv2.pointPolygonTest(safety_polygon, (last_x, last_y), measureDist=False)
                if distancia_poly >= 0:
                    categoria = "Proyección"
                else:
                    categoria = "Proyección peligrosa"

            # Guardamos el resultado en el diccionario
            resultados_json[str(track_id)] = {
                "clasificacion": categoria,
                "distancia_m": round(distancia_m, 2),
                "puntos": puntos_lista 
            }

        # 5. Guardar los resultados en un archivo JSON físico
        output_path = Path("temp_videos") / self.output_filename
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultados_json, f, indent=4, ensure_ascii=False)
            
        print(f"[{self.name}] {len(resultados_json)} trayectorias procesadas (categorizadas y medidas).")

        # 6. Guardar en el contexto para otros nodos o la API
        context["json_resultados"] = resultados_json
        context["json_file_path"] = str(output_path)

        return context