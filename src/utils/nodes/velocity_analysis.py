import numpy as np
import logging
from scipy.stats import gaussian_kde
from scipy.optimize import curve_fit
from typing import Any, Dict

# Assume PipelineNode is imported from our base file
from .base import PipelineNode

logger = logging.getLogger(__name__)

# =====================================================================
# 1. MATHEMATICAL DOMAIN FUNCTIONS
# =====================================================================
def ideal_gaussian(x: np.ndarray, a: float, mu: float, sigma: float) -> np.ndarray:
    """Ideal Gaussian bell curve function."""
    return a * np.exp(-(x - mu)**2 / (2 * sigma**2))

# =====================================================================
# 2. PIPELINE NODES
# =====================================================================
class TrajectoryVelocityNode(PipelineNode):
    """
    Calculates the mean velocity for each distinct trajectory.
    Optimized using numpy lexsort and memory-view splitting.
    """
    def __init__(self, name: str = "VelocityExtractor"):
        super().__init__(name)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("final_trajectories")
        
        if tensor is None:
            context["error"] = f"[{self.name}] No trajectory tensor found in context."
            return context

        logger.info(f"[{self.name}] Calculating kinematics for trajectories...")
        
        # FAST VECTORIZATION: Sort by ID (col 0), then by Time (col 3)
        sorted_idx = np.lexsort((tensor[:, 3], tensor[:, 0]))
        sorted_tensor = tensor[sorted_idx]

        # Find where new IDs start to split the array instantly
        _, start_indices = np.unique(sorted_tensor[:, 0], return_index=True)
        trajectories = np.split(sorted_tensor, start_indices[1:])

        mean_velocities = []

        for traj in trajectories:
            if len(traj) < 2:
                continue
                
            dx = np.diff(traj[:, 1])
            dy = np.diff(traj[:, 2])
            dt = np.diff(traj[:, 3])
            
            dt[dt == 0] = 1.0  # Prevent division by zero
            
            v_inst = np.hypot(dx, dy) / dt
            mean_velocities.append(np.mean(v_inst))

        velocities_array = np.array(mean_velocities)
        
        if len(velocities_array) == 0:
            context["error"] = f"[{self.name}] Not enough valid trajectories to compute velocities."
            return context

        logger.info(f"[{self.name}] Successfully extracted {len(velocities_array)} mean velocities.")
        context["mean_velocities"] = velocities_array
        
        return context


class GaussianThresholdNode(PipelineNode):
    """
    Uses Kernel Density Estimation (KDE) and mathematical curve fitting
    to find the ideal velocity separation threshold.
    """
    def __init__(self, name: str = "GaussianThreshold", sigma_multiplier: float = 0.5):
        super().__init__(name)
        self.sigma_multiplier = sigma_multiplier

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        velocities = context.get("mean_velocities")
        
        if velocities is None:
            return context

        logger.info(f"[{self.name}] Applying KDE to calculate probability density...")
        
        try:
            # 1. KDE Calculation
            kde = gaussian_kde(velocities)
            x_kde = np.linspace(0, np.max(velocities), 1000)
            y_kde = kde(x_kde)

            # 2. Find the main peak (Mode)
            peak_idx = np.argmax(y_kde)
            estimated_mu = x_kde[peak_idx]
            estimated_amp = y_kde[peak_idx]

            # 3. Create a perfect symmetrical target using the left side of the peak
            logger.info(f"[{self.name}] Fitting Gaussian curve to low-energy lobe...")
            x_left = x_kde[:peak_idx]
            y_left = y_kde[:peak_idx]
            
            dist_left = estimated_mu - x_left
            x_right_symmetric = estimated_mu + dist_left[::-1]
            y_right_symmetric = y_left[::-1]
            
            x_target = np.concatenate((x_left, [estimated_mu], x_right_symmetric))
            y_target = np.concatenate((y_left, [estimated_amp], y_right_symmetric))

            # 4. Curve Fitting
            popt, _ = curve_fit(
                ideal_gaussian, 
                x_target, 
                y_target, 
                p0=[estimated_amp, estimated_mu, estimated_mu]
            )
            
            _, opt_mu, opt_sigma = popt
            opt_sigma = abs(opt_sigma) # Standard deviation is always positive
            
            # 5. Threshold Calculation
            ideal_threshold = opt_mu + (self.sigma_multiplier * opt_sigma)
            
            logger.info(f"[{self.name}] Fit Success: Mu={opt_mu:.2f}, Sigma={opt_sigma:.2f}")
            logger.info(f"[{self.name}] Calculated Cutoff Threshold: {ideal_threshold:.2f} px/f")
            
            # Save results back to context
            context["velocity_threshold"] = ideal_threshold
            context["gaussian_params"] = {"mu": opt_mu, "sigma": opt_sigma}
            
        except Exception as e:
            logger.error(f"[{self.name}] Gaussian curve fitting failed: {e}")
            context["error"] = f"Curve fitting failed: {e}"

        return context
    
class HighVelocityFilterNode(PipelineNode):
    """
    Filters the trajectories, keeping only those whose mean velocity 
    exceeds the calculated threshold (isolating rocks from smoke).
    """
    def __init__(self, name: str = "HighVelocityFilter", manual_threshold: float = None):
        super().__init__(name)
        self.manual_threshold = manual_threshold

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("final_trajectories")
        # Use dynamic threshold from Gaussian node, fallback to manual, fallback to 0
        threshold = context.get("velocity_threshold", self.manual_threshold) or 0.0
        
        if tensor is None:
            return context

        logger.info(f"[{self.name}] Filtering trajectories (Threshold: >= {threshold:.2f} px/f)...")
        
        # Fast Vectorization (Sorting and Splitting)
        sorted_idx = np.lexsort((tensor[:, 3], tensor[:, 0]))
        sorted_tensor = tensor[sorted_idx]
        _, start_indices = np.unique(sorted_tensor[:, 0], return_index=True)
        trajectories = np.split(sorted_tensor, start_indices[1:])

        valid_rocks = {}
        
        for traj in trajectories:
            if len(traj) < 2:
                continue
                
            dx = traj[-1, 1] - traj[0, 1]
            dy = traj[-1, 2] - traj[0, 2]
            dt = traj[-1, 3] - traj[0, 3]
            
            mean_velocity = np.hypot(dx, dy) / dt if dt > 0 else 0.0
            
            if mean_velocity >= threshold:
                traj_id = int(traj[0, 0])
                valid_rocks[traj_id] = traj
                
        logger.info(f"[{self.name}] Isolated {len(valid_rocks)} legitimate rocks.")
        
        context["filtered_rocks_dict"] = valid_rocks
        return context