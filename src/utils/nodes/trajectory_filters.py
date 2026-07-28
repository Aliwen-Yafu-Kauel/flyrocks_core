import numpy as np
import logging
from typing import Dict, Any
from .base import PipelineNode

logger = logging.getLogger(__name__)

class TortuosityCalculationNode(PipelineNode):
    def __init__(self, name: str = "12_TortuosityCalculation"):
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resultados = context.get("json_resultados")
        trajectories = context.get("filtered_rocks_dict") 
        
        if not resultados or not trajectories:
            print(f"[{self.name}] ⚠️ Faltan datos en el contexto para calcular.")
            return context

        # 1. Crear un mapa seguro de IDs forzando todo a string para que hagan "match"
        traj_map = {str(k): v for k, v in trajectories.items()}

        modificados = 0
        # 2. Iteramos directamente sobre los objetos JSON que queremos modificar
        for track_str, data in resultados.items():
            if track_str not in traj_map:
                data["tortuosidad"] = 1.0  # Por defecto si no se encuentra
                continue
                
            traj = traj_map[track_str]
            if len(traj) < 3:
                data["tortuosidad"] = 1.0
                modificados += 1
                continue
            
            # Usamos el array original de numpy (extremadamente rápido)
            x, y = traj[:, 1], traj[:, 2]
            displacement = np.hypot(x[-1] - x[0], y[-1] - y[0])
            
            if displacement == 0:
                data["tortuosidad"] = 1.0
            else:
                path_length = np.sum(np.hypot(np.diff(x), np.diff(y)))
                data["tortuosidad"] = round(float(path_length / displacement), 3)
                
            modificados += 1
            
        print(f"[{self.name}] ✅ Se calculó y añadió 'tortuosidad' a {modificados}/{len(resultados)} trayectorias.")
        
        # Sobreescribimos el contexto por seguridad
        context["json_resultados"] = resultados
        return context

class OriginAreaExpansionNode(PipelineNode):
    """
    Calcula la distancia máxima que voló una roca fuera del área de origen,
    expresada como un factor relativo respecto al diámetro equivalente de la voladura.
    """
    def __init__(self, name: str = "13_OriginAreaExpansion"):
        super().__init__(name)

    def _get_convex_hull_ccw(self, points: np.ndarray) -> np.ndarray:
        pts = np.unique(points.astype(np.float32), axis=0)
        if len(pts) < 3: return pts
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
        resultados = context.get("json_resultados")
        trajectories = context.get("filtered_rocks_dict")
        origin_zone = context.get("origin_zone")
        
        if not resultados or not trajectories:
            return context
            
        if origin_zone is None or len(origin_zone[0]) < 6:
            for data in resultados.values(): 
                data["escape_relativo"] = 0.0
            return context
            
        # Preparación de la geometría (1 sola vez por ejecución del nodo)
        flat_zone = np.array(origin_zone[0], dtype=np.float32).reshape(-1, 2)
        hull_pts = self._get_convex_hull_ccw(flat_zone)
        
        if len(hull_pts) < 3:
            for data in resultados.values(): 
                data["escape_relativo"] = 0.0
            return context

        V = hull_pts
        V_next = np.roll(hull_pts, shift=-1, axis=0)
        
        # Área del polígono (usada para calcular el diámetro equivalente)
        area = 0.5 * np.abs(np.sum(V[:, 0] * V_next[:, 1] - V[:, 1] * V_next[:, 0]))
        
        D = V_next - V
        distances = np.linalg.norm(D, axis=1)
        
        N = np.column_stack((D[:, 1], -D[:, 0]))
        to_centroid = np.mean(V, axis=0) - V
        N[np.sum(N * to_centroid, axis=1) > 0] *= -1
        
        distances[distances == 0] = 1e-6
        N_unit = (N / distances[:, np.newaxis]).astype(np.float32) 
        C = np.sum(V * N_unit, axis=1) 
        N_unit_T = N_unit.T 

        traj_map = {str(k): v for k, v in trajectories.items()}

        for track_str, data in resultados.items():
            if track_str not in traj_map:
                data["escape_relativo"] = 0.0
                continue
                
            traj = traj_map[track_str]
            if area <= 0 or len(traj) == 0:
                data["escape_relativo"] = 0.0
                continue
                
            points = traj[:, 1:3].astype(np.float32)
            max_dist_outside = np.max(points @ N_unit_T - C)
            
            if max_dist_outside <= 0:
                # La roca cayó dentro del polígono original
                data["escape_relativo"] = 0.0
            else:
                # 1. Calculamos el "Diámetro Equivalente" del área de origen (voladura)
                diametro_origen = 2 * np.sqrt(area / np.pi)
                
                # 2. Factor de escape relativo (Cuántas veces el diámetro voló hacia afuera)
                factor_escape = max_dist_outside / diametro_origen
                
                data["escape_relativo"] = round(float(factor_escape), 2)
                
        context["json_resultados"] = resultados
        return context
class TrajectorySmoothnessNode(PipelineNode):
    def __init__(self, name: str = "14_TrajectorySmoothness"):
        super().__init__(name)

    def _calc_2d_adjusted_r2(self, x: np.ndarray, y: np.ndarray, t: np.ndarray, degree: int) -> float:
        """
        Calcula el R2 ajustado evaluando el error 2D (distancia real en pixeles).
        """
        n = len(t)
        p = degree + 1  # Parámetros (2 para línea, 3 para parábola)
        
        # Si hay muy pocos puntos para el modelo, el R2 no es confiable
        if n <= p + 1: 
            return 0.0
            
        # 1. Ajuste matemático ultra rápido
        coef_x = np.polyfit(t, x, degree)
        coef_y = np.polyfit(t, y, degree)
        
        pred_x = np.polyval(coef_x, t)
        pred_y = np.polyval(coef_y, t)
        
        # 2. Suma de Errores Cuadráticos (Distancia geométrica real 2D)
        sse = np.sum((x - pred_x)**2 + (y - pred_y)**2)
        
        # 3. Suma Total de Cuadrados (Varianza natural del vuelo)
        sst = np.sum((x - np.mean(x))**2 + (y - np.mean(y))**2)
        
        if sst == 0:
            return 0.0
            
        # R2 Clásico
        r2 = 1.0 - (sse / sst)
        
        # R2 Ajustado: Penaliza modelos si no hay suficientes puntos para respaldarlos
        r2_adj = 1.0 - ((1.0 - r2) * (n - 1) / (n - p))
        
        return float(r2_adj)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resultados = context.get("json_resultados")
        trajectories = context.get("filtered_rocks_dict")
        
        if not resultados or not trajectories:
            return context

        traj_map = {str(k): v for k, v in trajectories.items()}

        for track_str, data in resultados.items():
            if track_str not in traj_map:
                data["r2_score"] = 0.0
                continue
                
            traj = traj_map[track_str]
            n_puntos = len(traj)
            
            # Exigimos al menos 5 puntos. Un zigzag de 4 puntos 
            # engañaría fácilmente a un ajuste parabólico.
            if n_puntos < 5:
                data["r2_score"] = 0.0
                continue
            
            # Extracción inmediata sin copias de memoria
            x = traj[:, 1]
            y = traj[:, 2]
            t = np.arange(n_puntos)
            
            try:
                # Evaluación
                r2_lin = self._calc_2d_adjusted_r2(x, y, t, degree=1)
                r2_par = self._calc_2d_adjusted_r2(x, y, t, degree=2)
                
                mejor_r2 = max(r2_lin, r2_par)
                
                # Los modelos pueden dar R2 negativo si la predicción es peor que una línea plana.
                # Lo acotamos estrictamente entre 0.0 y 1.0
                data["r2_score"] = max(0.0, min(1.0, round(mejor_r2, 3)))
                
            except (np.linalg.LinAlgError, RuntimeWarning):
                data["r2_score"] = 0.0

        context["json_resultados"] = resultados
        return context