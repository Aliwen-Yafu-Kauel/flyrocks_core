import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.cluster import DBSCAN
import gc
import os
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Tuple
import logging
from pathlib import Path

# Assume PipelineNode is imported from our base file
from .base import PipelineNode

logger = logging.getLogger(__name__)

# =====================================================================
# 1. DOMAIN ENTITIES (Física Equilibrada 3D)
# =====================================================================

class KalmanFilter3D:
    """Modelo Cinemático 3D (Posición, Intensidad y Velocidad)."""
    def __init__(self, x: float, y: float, intensity: float):
        self.X = np.array([x, y, intensity, 0.0, 0.0, 0.0]) 
        self.P = np.eye(6) * 10.0 
        self.F = np.array([[1, 0, 0, 1, 0, 0], 
                           [0, 1, 0, 0, 1, 0], 
                           [0, 0, 1, 0, 0, 1], 
                           [0, 0, 0, 1, 0, 0], 
                           [0, 0, 0, 0, 1, 0], 
                           [0, 0, 0, 0, 0, 1]])
        self.H = np.array([[1, 0, 0, 0, 0, 0], 
                           [0, 1, 0, 0, 0, 0], 
                           [0, 0, 1, 0, 0, 0]])
        # Confianza equilibrada: Tolera temblor espacial (10), confía un poco menos en la luz (20)
        self.R = np.diag([10.0, 10.0, 20.0])  
        # Inercia pesada: Restringe cambios bruscos de velocidad (1.0)
        self.Q = np.diag([2.0, 2.0, 5.0, 1.0, 1.0, 2.0])  

    def predict(self) -> np.ndarray:
        self.X = self.F @ self.X
        self.P = self.F @ self.P @ self.F.T + self.Q
        return self.X[:3]

    def update(self, z: np.ndarray):
        y = z - (self.H @ self.X) 
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S) 
        self.X = self.X + (K @ y)
        self.P = (np.eye(6) - K @ self.H) @ self.P

    def get_kinematics(self) -> Tuple[float, float, float, float, float]:
        """Retorna x, y, intensidad, vx, vy."""
        return self.X[0], self.X[1], self.X[2], self.X[3], self.X[4]
        
    def get_smoothed_state(self) -> np.ndarray:
        """Retorna el vector filtrado [x, y, intensidad] para suavizar la exportación."""
        return self.X[:3]


class Trajectory:
    def __init__(self, traj_id: int, x: float, y: float, intensity: float, t: int):
        self.id = traj_id
        self.kalman = KalmanFilter3D(x, y, intensity)
        # History format: [x, y, frame, intensity, is_real]
        self.history = [(x, y, t, intensity, True)]
        self.lost_frames = 0
        self.is_active = True


# =====================================================================
# 2. SHARED UTILITIES (Needed for Multiprocessing)
# =====================================================================

def run_tracking_core(frames: np.ndarray, detections_by_frame: Dict[int, np.ndarray], 
                      max_lost_frames: int, dist_min: float = 20.0, dist_max: float = 60.0, 
                      peso_luz: float = 0.1, speed_multiplier: float = 2.0, 
                      angle_weight: float = 300.0, angle_power: float = 1.5) -> List[Trajectory]:
    """Tracker Húngaro Avanzado con Compuertas Dinámicas y Coherencia Direccional Suave."""
    trajectories = []
    current_id = 0
    
    for t in frames:
        detections = detections_by_frame[t]
        active_trajs = [tr for tr in trajectories if tr.is_active]
        
        # 1. Update State via Prediction
        [tr.kalman.predict() for tr in active_trajs]
        
        assignments = []
        unassigned_tr = list(range(len(active_trajs)))
        unassigned_det = list(range(len(detections)))
        
        if len(active_trajs) > 0 and len(detections) > 0:
            preds_pos = np.array([[tr.kalman.get_kinematics()[0], tr.kalman.get_kinematics()[1]] for tr in active_trajs])
            preds_int = np.array([tr.kalman.get_kinematics()[2] for tr in active_trajs])
            
            # Distancias Base
            dist_esp = np.linalg.norm(preds_pos[:, None, :] - detections[:, :2][None, :, :], axis=2)
            dist_int = np.abs(preds_int[:, None] - detections[:, 2][None, :])
            
            # Costo Base Cuadrático
            cost_matrix = (dist_esp**2) + (peso_luz * (dist_int**2))

            for i, tr in enumerate(active_trajs):
                px, py, pi, vx, vy = tr.kalman.get_kinematics()
                speed = np.hypot(vx, vy)
                
                # A) Adaptive Gating (Compuerta Elástica)
                gate = np.clip(speed * speed_multiplier, dist_min, dist_max)
                cost_matrix[i, dist_esp[i] > gate] = 1e5
                
                # B) Soft Directional Coherence (Penalización Angular)
                if speed > 2.0:
                    v_pred = np.array([vx, vy]) / speed
                    v_meas = detections[:, :2] - np.array([px, py])
                    norms = np.linalg.norm(v_meas, axis=1)
                    
                    with np.errstate(divide='ignore', invalid='ignore'):
                        v_meas_normed = v_meas / norms[:, None]
                        v_meas_normed[np.isnan(v_meas_normed)] = 0.0
                        
                    cos_theta = np.dot(v_meas_normed, v_pred)
                    cos_theta = np.clip(cos_theta, -1.0, 1.0) # Previene float bugs (NaN)
                    
                    # Solo aplica si el movimiento es significativo (>3 px)
                    valid_move = norms > 3.0
                    angle_penalty = np.where(valid_move, angle_weight * ((1.0 - cos_theta) ** angle_power), 0)
                    cost_matrix[i] += angle_penalty

            row_ind, col_ind = linear_sum_assignment(cost_matrix)
            for tr_idx, det_idx in zip(row_ind, col_ind):
                if cost_matrix[tr_idx, det_idx] < 1e5:
                    assignments.append((tr_idx, det_idx))
                    unassigned_tr.remove(tr_idx)
                    unassigned_det.remove(det_idx)

        for tr_idx, det_idx in assignments:
            tr = active_trajs[tr_idx]
            det = detections[det_idx]
            tr.kalman.update(det)
            
            # GUARDADO SUAVIZADO: Usamos el estado matemático, no la detección ruidosa
            smoothed = tr.kalman.get_smoothed_state()
            tr.history.append((smoothed[0], smoothed[1], t, smoothed[2], True))
            tr.lost_frames = 0

        for tr_idx in unassigned_tr:
            tr = active_trajs[tr_idx]
            tr.lost_frames += 1
            if tr.lost_frames > max_lost_frames:
                tr.is_active = False
            else:
                smoothed = tr.kalman.get_smoothed_state()
                tr.history.append((smoothed[0], smoothed[1], t, smoothed[2], False)) # Ghost frame

        for det_idx in unassigned_det:
            det = detections[det_idx]
            new_tr = Trajectory(current_id, det[0], det[1], det[2], t)
            trajectories.append(new_tr)
            current_id += 1

    return trajectories


def evaluate_trajectories(trajectories: List[Trajectory], min_frames: int = 10, min_dist: float = 30) -> Tuple[int, float, List[Trajectory]]:
    """Filters and calculates metrics for raw trajectories."""
    valid = []
    distances = []
    
    for tr in trajectories:
        # Check against index 4 (is_real)
        reales = [p for p in tr.history if p[4]]
        if len(reales) >= min_frames: 
            arr = np.array(reales)
            dist = np.hypot(arr[-1, 0] - arr[0, 0], arr[-1, 1] - arr[0, 1])
            if dist >= min_dist: 
                distances.append(dist)
                valid.append(tr)
                
    mean_dist = np.mean(distances) if distances else 0.0
    return len(valid), mean_dist, valid


def _parallel_worker(args):
    """Top-level function required for ProcessPoolExecutor serialization."""
    frames, detections, patience, cfg = args
    raw_trajs = run_tracking_core(frames, detections, patience, **cfg)
    count, mean_dist, _ = evaluate_trajectories(raw_trajs)
    return patience, count, mean_dist


# ===============================
# 3. PIPELINE NODES 
# ===============================

class EnergyPercentileFilterNode(PipelineNode):
    """Filters the raw 4D tensor keeping only the highest energy events."""
    def __init__(self, name: str = "EnergyFilter", percentile: float = 96.0):
        super().__init__(name)
        self.percentile = percentile

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        if tensor is None:
            context["error"] = f"[{self.name}] No input tensor found in context."
            return context

        threshold = np.percentile(np.abs(tensor[:, 3]), self.percentile)
        filtered_tensor = tensor[np.abs(tensor[:, 3]) >= threshold].copy()
        
        logger.info(f"[{self.name}] Filtered tensor (>{self.percentile}th pct): {len(tensor)} -> {len(filtered_tensor)} events.")
        
        context["tensor_raw"] = filtered_tensor
        return context


class DBSCANClusteringNode(PipelineNode):
    """Groups pixel events into spatial centroids per frame using X, Y, and Light."""
    def __init__(self, name: str = "DBSCAN_Clustering", eps: float = 5.0, min_samples: int = 1, max_samples: int = 50, peso_luz: float = 0.1):
        super().__init__(name)
        self.eps = eps
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.peso_luz = peso_luz

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        if tensor is None:
            return context

        frames_unique = np.sort(np.unique(tensor[:, 2]))[::-1]
        detections_by_frame = {}
        
        for t in frames_unique:
            points = tensor[tensor[:, 2] == t][:, [0, 1, 3]]
            detections = []
            if len(points) > 0:
                points_w = points.astype(float)
                points_w[:, 2] *= self.peso_luz
                
                clustering = DBSCAN(eps=self.eps, min_samples=self.min_samples).fit(points_w)
                for label in np.unique(clustering.labels_):
                    if label == -1: continue
                    mask = clustering.labels_ == label
                    # CORRECCIÓN: Bloquear clústeres masivos (humo)
                    if np.sum(mask) <= self.max_samples:
                        centroid = np.mean(points[mask], axis=0)
                        detections.append(centroid)
                    
            detections_by_frame[t] = np.array(detections)
            
        logger.info(f"[{self.name}] Extracted centroids for {len(frames_unique)} active frames.")
        
        context["unique_frames"] = frames_unique
        context["detections_by_frame"] = detections_by_frame
        return context

class GridSearchNode(PipelineNode):
    """Runs a Multiprocessing Grid Search to find the optimal 'max_lost_frames'."""
    def __init__(self, name: str = "GridSearchOptimizer", grid: List[int] = None, cores: int = 4):
        super().__init__(name)
        _default_grid = list(range(5, 15)) + list(range(15, 46, 2))
        self.grid = grid or _default_grid
        self.cores = cores
        
        # Configuración V4 (Física Equilibrada)
        self.tracking_cfg = {
            'dist_min': 20.0,
            'dist_max': 60.0,
            'peso_luz': 0.1,
            'speed_multiplier': 2.0,
            'angle_weight': 300.0,
            'angle_power': 1.5
        }

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        frames = context.get("unique_frames")
        detections = context.get("detections_by_frame")
        
        if frames is None or detections is None:
            context["error"] = f"[{self.name}] Missing clustering data."
            return context

        logger.info(f"[{self.name}] Starting Grid Search on {len(self.grid)} values using {self.cores} cores...")
        
        tasks = [(frames, detections, patience, self.tracking_cfg) for patience in self.grid]
        results_grid = {}
        
        t_start = time.time()
        with ProcessPoolExecutor(max_workers=self.cores) as executor:
            for patience, count, mean_dist in executor.map(_parallel_worker, tasks):
                results_grid[patience] = {'count': count, 'distance': mean_dist}
                
        logger.info(f"[{self.name}] Optimization finished in {time.time() - t_start:.1f}s.")
        
        optimal_patience = max(results_grid.keys(), key=lambda k: results_grid[k]['count'])
        logger.info(f"[{self.name}] Optimal patience found: {optimal_patience} frames.")
        
        context["optimal_patience"] = optimal_patience
        context["tracking_cfg"] = self.tracking_cfg
        context["optimization_metrics"] = results_grid 
        return context


class KalmanTrackerNode(PipelineNode):
    """Executes the final Multi-Object Tracking using the optimized parameters."""
    def __init__(self, name: str = "Kalman_MOT"):   
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        frames = context.get("unique_frames")
        detections = context.get("detections_by_frame")
        patience = context.get("optimal_patience", 15)
        cfg = context.get("tracking_cfg", {})
        
        if frames is None:
            return context

        logger.info(f"[{self.name}] Running final V4 tracking with patience={patience}...")
        raw_trajectories = run_tracking_core(frames, detections, patience, **cfg)
        
        context["raw_trajectories"] = raw_trajectories
        return context


class TrajectoryCleanerNode(PipelineNode):
    """Filters out noise, trims ghost frames, and formats output for export."""
    def __init__(self, name: str = "TrajectoryCleaner"):
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        raw_trajs = context.get("raw_trajectories")
        if raw_trajs is None:
            return context

        valid_count, mean_dist, valid_trajs = evaluate_trajectories(raw_trajs)
        logger.info(f"[{self.name}] Quality Check: {valid_count} valid trajectories (Mean Dist: {mean_dist:.2f} px).")

        export_data = []
        clean_id = 0 
        
        for tr in valid_trajs:
            history = np.array(tr.history)
            
            # CORRECCIÓN: Extraer estrictamente las filas donde 'is_real' es True
            reales = history[history[:, 4] == 1.0]
            
            if len(reales) < 3: # Must have at least 3 real observations
                continue
                
            ids = np.full((len(reales), 1), clean_id)
            final_trace = np.column_stack((ids, reales[:, :4]))
            
            # Ensure chronological order (since we track backwards)
            final_trace = final_trace[final_trace[:, 3].argsort()]
            
            export_data.append(final_trace)
            clean_id += 1
            
        if export_data:
            final_tensor = np.vstack(export_data)
            context["final_trajectories"] = final_tensor
            logger.info(f"[{self.name}] Created final tensor. Total exported objects: {clean_id}")
        else:
            context["final_trajectories"] = None
            logger.warning(f"[{self.name}] No trajectories survived cleaning.")

        return context

    def save_to_disk(self, tensor: np.ndarray, output_path: str | Path):
        """Utility method to persist the cleaned trajectories."""
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(out_path, tensor)
        logger.info(f"Saved final trajectories to: {out_path}")
    """Filters out noise, trims ghost frames, and formats output for export."""
    def __init__(self, name: str = "TrajectoryCleaner"):
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        raw_trajs = context.get("raw_trajectories")
        if raw_trajs is None:
            return context

        valid_count, mean_dist, valid_trajs = evaluate_trajectories(raw_trajs)
        logger.info(f"[{self.name}] Quality Check: {valid_count} valid trajectories (Mean Dist: {mean_dist:.2f} px).")

        export_data = []
        clean_id = 0 
        
        for tr in valid_trajs:
            history = np.array(tr.history)
            
            # Index 4 is the 'is_real' boolean flag
            real_indices = np.where(history[:, 4] == 1.0)[0]
            
            if len(real_indices) < 3: # Must have at least 3 real observations
                continue
                
            last_real_idx = real_indices[-1]
            trimmed_history = history[:last_real_idx + 1]
            
            # Format: [ID, X, Y, Frame, Intensity] -> history slices [:, :4] handles X, Y, T, I
            ids = np.full((len(trimmed_history), 1), clean_id)
            final_trace = np.column_stack((ids, trimmed_history[:, :4]))
            
            # Ensure chronological order (since we track backwards)
            final_trace = final_trace[final_trace[:, 3].argsort()]
            
            export_data.append(final_trace)
            clean_id += 1
            
        if export_data:
            final_tensor = np.vstack(export_data)
            context["final_trajectories"] = final_tensor
            logger.info(f"[{self.name}] Created final tensor. Total exported objects: {clean_id}")
        else:
            context["final_trajectories"] = None
            logger.warning(f"[{self.name}] No trajectories survived cleaning.")

        return context

    def save_to_disk(self, tensor: np.ndarray, output_path: str | Path):
        """Utility method to persist the cleaned trajectories."""
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(out_path, tensor)
        logger.info(f"Saved final trajectories to: {out_path}")