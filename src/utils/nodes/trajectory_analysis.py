import numpy as np
import matplotlib.pyplot as plt
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
# 1. DOMAIN ENTITIES (Pure Mathematics & State)
# =====================================================================
class KalmanFilter2D:
    def __init__(self, x: float, y: float):
        self.X = np.array([x, y, 0.0, 0.0]) 
        self.P = np.eye(4) * 10.0 
        self.F = np.array([[1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]])
        self.R = np.eye(2) * 5.0  
        self.Q = np.eye(4) * 1.0  

    def predict(self) -> np.ndarray:
        self.X = self.F @ self.X
        self.P = self.F @ self.P @ self.F.T + self.Q
        return self.X[:2]

    def update(self, z: np.ndarray):
        y = z - (self.H @ self.X) 
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S) 
        self.X = self.X + (K @ y)
        self.P = (np.eye(4) - K @ self.H) @ self.P


class Trajectory:
    def __init__(self, traj_id: int, x: float, y: float, t: int):
        self.id = traj_id
        self.kalman = KalmanFilter2D(x, y)
        self.history = [(x, y, t, True)]
        self.lost_frames = 0
        self.is_active = True


# =====================================================================
# 2. SHARED UTILITIES (Needed for Multiprocessing)
# =====================================================================
def run_tracking_core(frames: np.ndarray, detections_by_frame: Dict[int, np.ndarray], max_lost_frames: int, max_dist: float = 30.0) -> List[Trajectory]:
    """Core tracking logic using Kalman + Hungarian Algorithm."""
    trajectories = []
    current_id = 0
    
    for t in frames:
        detections = detections_by_frame[t]
        active_trajs = [tr for tr in trajectories if tr.is_active]
        predictions = np.array([tr.kalman.predict() for tr in active_trajs])

        assignments = []
        unassigned_tr = list(range(len(active_trajs)))
        unassigned_det = list(range(len(detections)))
        
        if len(predictions) > 0 and len(detections) > 0:
            cost_matrix = np.linalg.norm(predictions[:, None, :] - detections[None, :, :], axis=2)
            row_ind, col_ind = linear_sum_assignment(cost_matrix)
            for tr_idx, det_idx in zip(row_ind, col_ind):
                if cost_matrix[tr_idx, det_idx] <= max_dist:
                    assignments.append((tr_idx, det_idx))
                    unassigned_tr.remove(tr_idx)
                    unassigned_det.remove(det_idx)

        for tr_idx, det_idx in assignments:
            tr = active_trajs[tr_idx]
            det = detections[det_idx]
            tr.kalman.update(det)
            tr.history.append((det[0], det[1], t, True))
            tr.lost_frames = 0

        for tr_idx in unassigned_tr:
            tr = active_trajs[tr_idx]
            tr.lost_frames += 1
            if tr.lost_frames > max_lost_frames:
                tr.is_active = False
            else:
                pos = tr.kalman.X[:2]
                tr.history.append((pos[0], pos[1], t, False)) # Ghost frame

        for det_idx in unassigned_det:
            det = detections[det_idx]
            new_tr = Trajectory(current_id, det[0], det[1], t)
            trajectories.append(new_tr)
            current_id += 1

    return trajectories

def evaluate_trajectories(trajectories: List[Trajectory], min_frames: int = 10, min_dist: float = 30) -> Tuple[int, float, List[Trajectory]]:
    """Filters and calculates metrics for raw trajectories."""
    valid = []
    distances = []
    
    for tr in trajectories:
        if len(tr.history) >= min_frames: 
            arr = np.array(tr.history)
            dist = np.hypot(arr[-1, 0] - arr[0, 0], arr[-1, 1] - arr[0, 1])
            if dist >= min_dist: 
                distances.append(dist)
                valid.append(tr)
                
    mean_dist = np.mean(distances) if distances else 0.0
    return len(valid), mean_dist, valid

def _parallel_worker(args):
    """Top-level function required for ProcessPoolExecutor serialization."""
    frames, detections, patience, max_dist = args
    raw_trajs = run_tracking_core(frames, detections, patience, max_dist)
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

        threshold = np.percentile(tensor[:, 3], self.percentile)
        filtered_tensor = tensor[tensor[:, 3] >= threshold].copy()
        
        logger.info(f"[{self.name}] Filtered tensor (>{self.percentile}th pct): {len(tensor)} -> {len(filtered_tensor)} events.")
        
        context["tensor_raw"] = filtered_tensor
        return context


class DBSCANClusteringNode(PipelineNode):
    """Groups pixel events into spatial centroids per frame."""
    def __init__(self, name: str = "DBSCAN_Clustering", eps: float = 5.0, min_samples: int = 1):
        super().__init__(name)
        self.eps = eps
        self.min_samples = min_samples

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        if tensor is None:
            return context

        frames_unique = np.sort(np.unique(tensor[:, 2]))[::-1]
        detections_by_frame = {}
        
        for t in frames_unique:
            points = tensor[tensor[:, 2] == t][:, :2]
            detections = []
            if len(points) > 0:
                clustering = DBSCAN(eps=self.eps, min_samples=self.min_samples).fit(points)
                for label in np.unique(clustering.labels_):
                    if label == -1: continue
                    centroid = np.mean(points[clustering.labels_ == label], axis=0)
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

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        frames = context.get("unique_frames")
        detections = context.get("detections_by_frame")
        
        if frames is None or detections is None:
            context["error"] = f"[{self.name}] Missing clustering data."
            return context

        logger.info(f"[{self.name}] Starting Grid Search on {len(self.grid)} values using {self.cores} cores...")
        
        tasks = [(frames, detections, patience, 30.0) for patience in self.grid]
        results_grid = {}
        
        t_start = time.time()
        with ProcessPoolExecutor(max_workers=self.cores) as executor:
            for patience, count, mean_dist in executor.map(_parallel_worker, tasks):
                results_grid[patience] = {'count': count, 'distance': mean_dist}
                
        logger.info(f"[{self.name}] Optimization finished in {time.time() - t_start:.1f}s.")
        
        optimal_patience = max(results_grid.keys(), key=lambda k: results_grid[k]['count'])
        logger.info(f"[{self.name}] Optimal patience found: {optimal_patience} frames.")
        
        context["optimal_patience"] = optimal_patience
        context["optimization_metrics"] = results_grid 
        return context

class KalmanTrackerNode(PipelineNode):
    """Executes the final Multi-Object Tracking using the optimized parameters."""
    def __init__(self, name: str = "Kalman_MOT"):   
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        frames = context.get("unique_frames")
        detections = context.get("detections_by_frame")
        patience = context.get("optimal_patience", 15) # Default fallback
        
        if frames is None:
            return context

        logger.info(f"[{self.name}] Running final tracking with patience={patience}...")
        raw_trajectories = run_tracking_core(frames, detections, max_lost_frames=patience)
        
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

        # 1. Filter out short or static trajectories
        valid_count, mean_dist, valid_trajs = evaluate_trajectories(raw_trajs)
        logger.info(f"[{self.name}] Quality Check: {valid_count} valid trajectories (Mean Dist: {mean_dist:.2f} px).")

        # 2. Trim trailing ghost frames & format as tensor
        export_data = []
        clean_id = 0 
        
        for tr in valid_trajs:
            history = np.array(tr.history)
            real_indices = np.where(history[:, 3] == 1.0)[0]
            
            if len(real_indices) < 2:
                continue
                
            last_real_idx = real_indices[-1]
            trimmed_history = history[:last_real_idx + 1]
            
            ids = np.full((len(trimmed_history), 1), clean_id)
            final_trace = np.column_stack((ids, trimmed_history[:, :3]))
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