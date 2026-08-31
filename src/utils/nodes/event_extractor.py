import cv2
import numpy as np
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .base import PipelineNode

logger = logging.getLogger(__name__)

class EventExtractorNode(PipelineNode):

    # Fuera de la cache: su salida es el tensor crudo (~35 M eventos, del orden
    # de 1 GB) y guardarlo no compensa. No se pierde nada: el nodo siguiente
    # filtra ese tensor a ~1.4 M eventos y SU cache ya contiene todo lo que
    # hace falta para seguir, asi que al reanudar este nodo ni se ejecuta.
    cacheable = False

    def __init__(
        self, 
        name: str = "EventExtractor4D",
        noise_threshold: int = 8, 
        blur_kernel: Tuple[int, int] = (3, 3),
        fallback_video_path: Optional[str | Path] = None,
        output_mask_filename: str = "mascara_cambios.png"
    ):
        super().__init__(name)
        self.noise_threshold = noise_threshold
        self.blur_kernel = blur_kernel
        self.fallback_video_path = Path(fallback_video_path) if fallback_video_path else None
        self.output_mask_filename = output_mask_filename
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
        frame_index = 2
        
        try:
            ret, previous_frame = cap.read()
            if not ret: return context

            # Inicializar la máscara de acumulación
            height, width = previous_frame.shape[:2]
            max_change_mask = np.zeros((height, width), dtype=np.uint8)

            previous_gray = cv2.cvtColor(previous_frame, cv2.COLOR_BGR2GRAY)
            previous_gray = cv2.GaussianBlur(previous_gray, self.blur_kernel, 0)
            
            while True:
                ret, current_frame = cap.read()
                if not ret: break

                current_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
                current_gray = cv2.GaussianBlur(current_gray, self.blur_kernel, 0)
                
                difference = cv2.absdiff(previous_gray, current_gray)
                
                # OPTIMIZACIÓN 1: Operación In-Place. Guardamos la diferencia directamente sobre la memoria de max_change_mask.
                cv2.max(max_change_mask, difference, dst=max_change_mask)

                ys, xs = np.nonzero(difference > self.noise_threshold)
                
                if ys.size > 0:
                    intensities = difference[ys, xs]
                    
                    # OPTIMIZACIÓN 2: Early Downcasting. Convertimos int64 a uint16 antes del apilado.
                    xs = xs.astype(np.uint16)
                    ys = ys.astype(np.uint16)
                    ts = np.full(ys.size, frame_index, dtype=np.uint16)
                    
                    # Como xs, ys y ts son uint16, e intensities es uint8, el resultado será 100% uint16 (8 bytes por fila).
                    frame_events = np.column_stack((xs, ys, ts, intensities))
                    event_cloud_4d.append(frame_events)

                previous_gray = current_gray
                frame_index += 1

            # --- MEJORA VISUAL: Traslación Aditiva Alpha In-Place ---
            v_max = int(np.max(max_change_mask))
            if 0 < v_max < 255:
                alpha = 255 - v_max
                # OPTIMIZACIÓN 3: Suma nativa con máscara usando C++ bajo el capó sin crear tensores intermedios
                mask_activa = (max_change_mask > 0).astype(np.uint8)
                cv2.add(max_change_mask, alpha, dst=max_change_mask, mask=mask_activa)
                logger.info(f"[{self.name}] Máscara mejorada con traslación alpha={alpha}")

            # Guardar en disco y añadir al contexto
            mask_path = video_path.parent / self.output_mask_filename
            cv2.imwrite(str(mask_path), max_change_mask)
            context["change_mask_path"] = str(mask_path)
            logger.info(f"[{self.name}] Máscara de cambios absolutos guardada en: {mask_path}")

        except Exception as e:
            context["error"] = str(e)
        finally:
            logger.info(f"[{self.name}] Frames totales procesados: {frame_index}")
            cap.release()

        if event_cloud_4d:
            # OPTIMIZACIÓN 4: El concatenado final ahora ensambla piezas ligeras (uint16) directamente.
            self.tensor_raw = np.concatenate(event_cloud_4d, axis=0)
            context["tensor_raw"] = self.tensor_raw
            logger.info(f"[{self.name}] Extracted {len(self.tensor_raw):,} events successfully (Dtype: {self.tensor_raw.dtype}).")
        else:
            context["tensor_raw"] = None

        return context