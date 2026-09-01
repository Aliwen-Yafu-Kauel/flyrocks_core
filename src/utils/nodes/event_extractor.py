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
        name: str = "EventExtractor4D_Signed",
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

            # Inicializar la máscara de acumulación para la UI (absoluta)
            height, width = previous_frame.shape[:2]
            max_change_mask = np.zeros((height, width), dtype=np.uint8)

            # LÓGICA DE LABORATORIO: Convertimos a int16 desde el inicio para permitir restas con signo
            previous_gray = cv2.cvtColor(previous_frame, cv2.COLOR_BGR2GRAY)
            previous_gray = cv2.GaussianBlur(previous_gray, self.blur_kernel, 0).astype(np.int16)
            
            while True:
                ret, current_frame = cap.read()
                if not ret: break

                current_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
                current_gray = cv2.GaussianBlur(current_gray, self.blur_kernel, 0).astype(np.int16)
                
                # Diferencia exacta (Actual - Anterior) para conservar el SIGNO (+/-)
                difference = current_gray - previous_gray
                
                # Matriz de magnitudes absolutas para filtrar ruido y generar la máscara visual
                abs_diff = np.abs(difference)
                
                # Guardamos la magnitud directamente sobre la memoria de la máscara (requiere uint8)
                cv2.max(max_change_mask, abs_diff.astype(np.uint8), dst=max_change_mask)

                ys, xs = np.nonzero(abs_diff > self.noise_threshold)
                
                if ys.size > 0:
                    # Extraemos la intensidad CON SIGNO de la matriz difference, no del abs_diff
                    intensities = difference[ys, xs]
                    
                    # OPTIMIZACIÓN DE RAM: Convertimos a int16 (con signo).
                    # Soporta coordenadas hasta 32K y mantiene los negativos fotométricos intactos.
                    xs = xs.astype(np.int16)
                    ys = ys.astype(np.int16)
                    ts = np.full(ys.size, frame_index, dtype=np.int16)
                    
                    frame_events = np.column_stack((xs, ys, ts, intensities))
                    event_cloud_4d.append(frame_events)

                previous_gray = current_gray
                frame_index += 1

            # --- MEJORA VISUAL: Traslación Aditiva Alpha In-Place ---
            v_max = int(np.max(max_change_mask))
            if 0 < v_max < 255:
                alpha = 255 - v_max
                mask_activa = (max_change_mask > 0).astype(np.uint8)
                cv2.add(max_change_mask, alpha, dst=max_change_mask, mask=mask_activa)
                logger.info(f"[{self.name}] Máscara mejorada con traslación alpha={alpha}")

            # Guardar máscara en disco
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
            # Concatenado final ensamblando bloques int16 nativos
            self.tensor_raw = np.concatenate(event_cloud_4d, axis=0)
            context["tensor_raw"] = self.tensor_raw
            logger.info(f"[{self.name}] Extracted {len(self.tensor_raw):,} SIGNED events successfully (Dtype: {self.tensor_raw.dtype}).")
        else:
            context["tensor_raw"] = None

        return context