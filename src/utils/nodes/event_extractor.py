import cv2
import numpy as np
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .base import PipelineNode

logger = logging.getLogger(__name__)

class EventExtractorNode(PipelineNode):
    
    def __init__(
        self, 
        name: str = "EventExtractor4D",
        noise_threshold: int = 8, 
        blur_kernel: Tuple[int, int] = (3, 3),
        fallback_video_path: Optional[str | Path] = None
    ):
        super().__init__(name)
        self.noise_threshold = noise_threshold
        self.blur_kernel = blur_kernel
        self.fallback_video_path = Path(fallback_video_path) if fallback_video_path else None
        self.tensor_raw: Optional[np.ndarray] = None

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        path_str = context.get("video_path", self.fallback_video_path)
        if not path_str:
            context["error"] = "Missing 'video_path'"
            return context
            
        video_path = Path(path_str)
        if not video_path.exists():
            context["error"] = f"File not found: {video_path}"
            return context

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            context["error"] = "Failed to open video."
            return context

        event_cloud_4d = []
        
        try:
            ret, previous_frame = cap.read()
            if not ret: return context

            previous_gray = cv2.cvtColor(previous_frame, cv2.COLOR_BGR2GRAY)
            previous_gray = cv2.GaussianBlur(previous_gray, self.blur_kernel, 0)
            
            frame_index = 2
            while True:
                ret, current_frame = cap.read()
                if not ret: break

                current_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
                current_gray = cv2.GaussianBlur(current_gray, self.blur_kernel, 0)
                
                difference = cv2.absdiff(previous_gray, current_gray)
                ys, xs = np.nonzero(difference > self.noise_threshold)
                
                if ys.size > 0:
                    intensities = difference[ys, xs]
                    ts = np.full(ys.size, frame_index, dtype=np.uint16)
                    frame_events = np.column_stack((xs, ys, ts, intensities))
                    event_cloud_4d.append(frame_events)

                previous_gray = current_gray
                frame_index += 1

        except Exception as e:
            context["error"] = str(e)
        finally:
            print(f"Frames totales procesados: {frame_index}") # <-- Añade esto
            cap.release()

        if event_cloud_4d:
            self.tensor_raw = np.concatenate(event_cloud_4d, axis=0)
            context["tensor_raw"] = self.tensor_raw
            logger.info(f"[{self.name}] Extracted {len(self.tensor_raw):,} events successfully.")
        else:
            context["tensor_raw"] = None

        return context