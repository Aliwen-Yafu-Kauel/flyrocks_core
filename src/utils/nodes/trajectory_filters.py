import numpy as np
import logging
from typing import Dict, Any
from .base import PipelineNode  # Asegúrate de que esta importación coincida con tu estructura

logger = logging.getLogger(__name__)

class ZigzagFilterNode(PipelineNode):
    """
    Filtra las trayectorias evaluando su 'tortuosidad' para eliminar aquellas con 
    comportamiento errático (zigzag). Conserva líneas rectas y curvas parabólicas.
    """
    def __init__(self, name: str = "ZigzagFilter", max_tortuosity: float = 1.25):
        super().__init__(name)
        # 1.0 es una recta perfecta. 1.25 permite curvas de vuelo balístico. >1.5 es ruido/zigzag.
        self.max_tortuosity = max_tortuosity

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        rocks_dict = context.get("filtered_rocks_dict")
        
        # Si no hay datos del nodo anterior, pasamos de largo
        if not rocks_dict:
            return context

        logger.info(f"[{self.name}] Evaluando linealidad (Umbral de tortuosidad <= {self.max_tortuosity})...")
        
        smooth_rocks = {}
        
        for traj_id, traj in rocks_dict.items():
            # Si tiene muy pocos puntos, asumimos que es válida (no hay datos suficientes para zigzag)
            if len(traj) < 3:
                smooth_rocks[traj_id] = traj
                continue
            
            # Extraer coordenadas X e Y (índices 1 y 2 de tu tensor)
            x = traj[:, 1]
            y = traj[:, 2]
            
            # 1. Desplazamiento Neto (Distancia en línea recta desde inicio a fin)
            displacement = np.hypot(x[-1] - x[0], y[-1] - y[0])
            
            # 2. Distancia Total Recorrida (Suma de los segmentos punto a punto)
            dx = np.diff(x)
            dy = np.diff(y)
            path_length = np.sum(np.hypot(dx, dy))
            
            # Evitar división por cero (si la trayectoria empieza y termina exactamente en el mismo pixel)
            if displacement == 0:
                continue 
                
            # 3. Cálculo de la Tortuosidad
            tortuosity = path_length / displacement
            
            # 4. Filtrado
            if tortuosity <= self.max_tortuosity:
                smooth_rocks[traj_id] = traj
                
        # Logs de resultados
        descartadas = len(rocks_dict) - len(smooth_rocks)
        logger.info(f"[{self.name}] Eliminadas {descartadas} trayectorias en zigzag.")
        logger.info(f"[{self.name}] Entregando {len(smooth_rocks)} trayectorias limpias al siguiente nodo.")
        
        # Sobrescribimos el diccionario en el contexto para que el TrajectoryCategorizationNode lo use
        context["filtered_rocks_dict"] = smooth_rocks
        
        return context
    
class OriginZoneFilterNode(PipelineNode):
    def __init__(self, name: str = "OriginZoneFilter", area_expansion_pct: float = 0.05):
        super().__init__(name)
        self.area_expansion_pct = area_expansion_pct

    def _get_convex_hull_ccw(self, points: np.ndarray) -> np.ndarray:
        pts = np.unique(points.astype(np.float32), axis=0)
        if len(pts) < 3:
            return pts
            
        ind = np.lexsort((pts[:, 1], pts[:, 0]))
        pts = pts[ind]
        
        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
        
        lower = []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
            
        upper = []
        for p in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
            
        return np.array(lower[:-1] + upper[:-1], dtype=np.float32)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        rocks_dict = context.get("filtered_rocks_dict")
        origin_zone = context.get("origin_zone")
        
        if not rocks_dict:
            return context
          
        if origin_zone is None or len(origin_zone[0]) < 6:
            logger.warning(f"[{self.name}] 'origin_zone' no es válido. Saltando filtro.")
            context["outside_origin_rocks_dict"] = rocks_dict
            return context
            
        flat_zone = np.array(origin_zone[0], dtype=np.float32)
        zone_pts = flat_zone.reshape(-1, 2)
        
        hull_pts = self._get_convex_hull_ccw(zone_pts)
        
        if len(hull_pts) < 3:
            context["outside_origin_rocks_dict"] = rocks_dict
            return context

        V = hull_pts
        V_next = np.roll(hull_pts, shift=-1, axis=0)
        
        # 1. Área del polígono (Shoelace) y Perímetro
        cross_products = V[:, 0] * V_next[:, 1] - V[:, 1] * V_next[:, 0]
        area = 0.5 * np.abs(np.sum(cross_products))
        
        D = V_next - V
        distances = np.linalg.norm(D, axis=1)
        perimeter = np.sum(distances)
        
        # 2. Delta dinámico
        dynamic_delta_px = (self.area_expansion_pct * area) / perimeter if perimeter > 0 else 0.0
        
        # 3. Normales exteriores garantizadas
        N = np.column_stack((D[:, 1], -D[:, 0]))
        centroid = np.mean(V, axis=0)
        to_centroid = centroid - V
        
        dot_prod = np.sum(N * to_centroid, axis=1)
        N[dot_prod > 0] *= -1
        
        norms = distances
        norms[norms == 0] = 1e-6
        N_unit = (N / norms[:, np.newaxis]).astype(np.float32) 
        
        # --- OPTIMIZACIONES CLAVE DE ÁLGEBRA LINEAL Y MEMORIA ---
        
        # A. Precalculamos (V • N) fuera del bucle. Esto es un array 1D de tamaño K (aristas).
        C = np.sum(V * N_unit, axis=1) 
        
        # B. Transponemos la normal una sola vez para la multiplicación matricial. Forma (2, K)
        N_unit_T = N_unit.T 
        
        filtered_rocks = {}
        
        for traj_id, traj in rocks_dict.items():
            # C. np.asarray() evita duplicar en memoria si el array ya era float32
            points = np.asarray(traj[:, 1:3], dtype=np.float32)
            
            if points.shape[1] != 2 or points.shape[0] == 0:
                continue
                
            # D. Multiplicación Matricial + Short-Circuit
            # points @ N_unit_T calcula (P • N) para TODOS los puntos contra TODAS las normales al mismo tiempo.
            # Al restar 'C', aplicamos broadcasting sin usar memoria extra.
            # np.any() detiene la evaluación entera devolviendo True si halla 1 solo punto fuera del delta.
            if np.any(points @ N_unit_T - C > dynamic_delta_px):
                filtered_rocks[traj_id] = traj
                
        context["outside_origin_rocks_dict"] = filtered_rocks
        return context