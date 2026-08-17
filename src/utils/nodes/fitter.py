import cv2
import pandas as pd
import numpy as np
import logging
from typing import Any, Dict

from .base import PipelineNode

logger = logging.getLogger(__name__)

class OriginPredictorNode(PipelineNode):
    def __init__(self, name: str = "OriginPredictor"):
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"[{self.name}] Prediciendo origen (Selección dinámica de modelo: Curva vs Recta)...")
        
        json_resultados = context.get("json_resultados", {})
        trayectorias = json_resultados.get("trayectorias", [])
        h_matrix_raw = context.get("h_matrix")
        csv_path = context.get("csv_file_path") 
        
        if not trayectorias or not h_matrix_raw or not csv_path:
            logger.warning(f"[{self.name}] Faltan datos (trayectorias, H o CSV). Saltando nodo.")
            return context
            
        # 1. Cargar malla de pozos
        try:
            df = pd.read_csv(csv_path)
            df.columns = df.columns.str.strip() 
            pozos = df[['Label', 'X', 'Y']].dropna().values
        except Exception as e:
            context["error"] = f"Error leyendo CSV de pozos: {e}"
            return context

        # 2. Calcular matriz inversa H^-1
        h_matrix = np.array(h_matrix_raw, dtype=np.float64).reshape(3, 3)
        try:
            h_inv = np.linalg.inv(h_matrix)
        except np.linalg.LinAlgError:
            context["error"] = "Matriz H no invertible."
            return context

        # 3. Procesar cada trayectoria
        for tray in trayectorias:
            raw_pts = tray.get('puntos') or tray.get('puntos_px') or tray.get('points', [])
            
            if len(raw_pts) < 2:
                continue
                
            pts_px = np.array(raw_pts, dtype=np.float32).reshape(-1, 1, 2)
            pts_metros = cv2.perspectiveTransform(pts_px, h_inv).reshape(-1, 2)
            
            # Parametrización
            t_real = np.arange(len(pts_metros))
            X = pts_metros[:, 0]
            Y = pts_metros[:, 1]
            
            # Varianza total para R^2
            ss_tot = np.sum((X - np.mean(X))**2) + np.sum((Y - np.mean(Y))**2)
            
            # --- COMPETENCIA DE MODELOS ---
            # Modelo A: Recta (Grado 1)
            p1_x = np.poly1d(np.polyfit(t_real, X, 1))
            p1_y = np.poly1d(np.polyfit(t_real, Y, 1))
            ss_res_1 = np.sum((X - p1_x(t_real))**2) + np.sum((Y - p1_y(t_real))**2)
            r2_1 = 1 - (ss_res_1 / ss_tot) if ss_tot > 0 else 0.0
            
            # Modelo B: Parábola (Grado 2)
            r2_2 = -float('inf')
            if len(pts_metros) >= 3:
                p2_x = np.poly1d(np.polyfit(t_real, X, 2))
                p2_y = np.poly1d(np.polyfit(t_real, Y, 2))
                ss_res_2 = np.sum((X - p2_x(t_real))**2) + np.sum((Y - p2_y(t_real))**2)
                r2_2 = 1 - (ss_res_2 / ss_tot) if ss_tot > 0 else 0.0
                
            # Selección del mejor modelo (Exigimos que la parábola mejore el R2 en al menos 1% para evitar overfitting)
            if r2_2 > (r2_1 + 0.01):
                best_px, best_py = p2_x, p2_y
                modelo_usado = "Parábola (Grado 2)"
            else:
                best_px, best_py = p1_x, p1_y
                modelo_usado = "Recta (Grado 1)"
                
            # --- EXTRAPOLACIÓN ---
            # Generamos 2000 puntos hacia el pasado
            t_pasado = np.linspace(0, -len(pts_metros) * 50, num=2000)
            curva_pasado = np.column_stack((best_px(t_pasado), best_py(t_pasado)))
            
            mejor_pozo = None
            min_dist = float('inf')
            
            # --- BÚSQUEDA DEL POZO MÁS CERCANO ---
            for pozo in pozos:
                pozo_id, pozo_x, pozo_y = pozo[0], pozo[1], pozo[2]
                punto_pozo = np.array([pozo_x, pozo_y])
                
                distancias_a_curva = np.linalg.norm(curva_pasado - punto_pozo, axis=1)
                distancia_minima = np.min(distancias_a_curva)
                
                if distancia_minima < min_dist:
                    min_dist = distancia_minima
                    mejor_pozo = {
                        "pozo_id": str(pozo_id),
                        "x": float(pozo_x),
                        "y": float(pozo_y),
                        "desviacion_metros": round(float(min_dist), 2),
                        "modelo_ajuste": modelo_usado
                    }
                        
            if mejor_pozo:
                tray["origen_probable"] = mejor_pozo

        json_resultados["trayectorias"] = trayectorias
        context["json_resultados"] = json_resultados
        
        logger.info(f"[{self.name}] Orígenes probables calculados exitosamente.")
        return context