import cv2
import numpy as np
import matplotlib.pyplot as plt
import logging
import time
from pathlib import Path
from typing import Any, Dict

from .base import PipelineNode

logger = logging.getLogger(__name__)

class VideoRendererNode(PipelineNode):
    """
    Renders the final video, overlaying the filtered trajectories onto the original footage.
    """
    def __init__(self, name: str = "VideoRenderer", alpha: float = 0.8, output_filename: str = "final_render.mp4"):
        super().__init__(name)
        self.alpha = alpha
        self.output_filename = output_filename

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        video_in_path = context.get("video_path")
        valid_rocks = context.get("filtered_rocks_dict")
        threshold = context.get("velocity_threshold", 0.0)
        
        if not video_in_path or valid_rocks is None:
            context["error"] = f"[{self.name}] Missing video path or filtered rocks in context."
            return context

        video_in_path = Path(video_in_path)
        video_out_path = video_in_path.parent / self.output_filename

        logger.info(f"[{self.name}] Starting Render: Original Video + Trajectories")
        
        # 1. Color mapping assignment
        cmap = plt.get_cmap('tab20')
        colors_bgr = {}
        for idx, tr_id in enumerate(valid_rocks.keys()):
            rgba = cmap(idx % 20)
            colors_bgr[tr_id] = (int(rgba[2]*255), int(rgba[1]*255), int(rgba[0]*255))

        # 2. Video Initialization
        cap = cv2.VideoCapture(str(video_in_path))
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(str(video_out_path), fourcc, fps, (width, height))

        t_start = time.time()
        
        # 3. Render Loop
        for frame_idx in range(total_frames):
            ret, frame = cap.read()
            if not ret: break
                
            overlay = frame.copy()
            rocks_on_screen = 0
            
            for tr_id, trace in valid_rocks.items():
                past_points = trace[trace[:, 3] <= frame_idx]
                
                if len(past_points) > 1:
                    rocks_on_screen += 1
                    color = colors_bgr[tr_id]
                    
                    pts = past_points[:, 1:3].astype(np.int32).reshape((-1, 1, 2))
                    cv2.polylines(overlay, [pts], isClosed=False, color=color, thickness=2)
                    
                    head_x, head_y = pts[-1][0]
                    cv2.circle(overlay, (head_x, head_y), 3, color, -1)

            cv2.addWeighted(overlay, self.alpha, frame, 1 - self.alpha, 0, frame)

            # Minimalist HUD
            cv2.putText(frame, f'Frame: {frame_idx}', (30, 40), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(frame, f'Rocks (V >= {threshold:.1f}): {rocks_on_screen}', (30, 80), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)

            out.write(frame)
            
            if frame_idx > 0 and frame_idx % 50 == 0:
                vel = frame_idx / (time.time() - t_start)
                logger.info(f"[{self.name}] Rendered {frame_idx}/{total_frames} frames ({vel:.1f} fps)")

        cap.release()
        out.release()
        
        logger.info(f"[{self.name}] Render complete. Saved to: {video_out_path}")
        context["final_video_path"] = video_out_path
        
        return context