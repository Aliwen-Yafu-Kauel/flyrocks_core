import os
import cv2
import numpy as np
import logging
from typing import Any, Dict
from .base import PipelineNode
from utils.database import Job, engine

logger = logging.getLogger(__name__)

class PercentilePreviewNode(PipelineNode):
    def __init__(self, name: str = "1.8_PercentilePreview", output_filename: str = "mascara_shifted.png"):
        super().__init__(name)
        self.output_filename = output_filename

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        job_id = context.get("job_id")
        
        if tensor is None or len(tensor) == 0:
            logger.warning(f"[{self.name}] Tensor vacío. Omitiendo generación de vista previa.")
            return context

        # 1. LA ESTADÍSTICA REAL (Población 4D completa con signos normalizados)
        intensidades_abs = np.abs(tensor[:, 3])
        
        # 2. LA VISUALIZACIÓN (Colapso espacial 2D solo para la imagen)
        max_x = int(np.max(tensor[:, 0])) + 1
        max_y = int(np.max(tensor[:, 1])) + 1
        
        canvas_raw = np.zeros((max_y, max_x), dtype=np.float32)
        np.maximum.at(canvas_raw, (tensor[:, 1].astype(int), tensor[:, 0].astype(int)), intensidades_abs)
        
        mask_validos = canvas_raw > 0
        intensidades_pantalla = canvas_raw[mask_validos]
        
        if len(intensidades_pantalla) == 0:
            logger.warning(f"[{self.name}] No hay intensidades válidas.")
            return context

        # 3. Traslación aditiva para la imagen (+shift = 255 - max_global)
        max_global = float(np.max(intensidades_abs))
        shift = 255.0 - max_global
        
        canvas_shifted = np.zeros_like(canvas_raw, dtype=np.uint8)
        canvas_shifted[mask_validos] = np.clip(canvas_raw[mask_validos] + shift, 0, 255).astype(np.uint8)

        # 4. Guardar artefactos en el directorio del job
        job_dir = context.get("job_dir")
        if not job_dir and "video_path" in context:
            job_dir = os.path.dirname(context["video_path"])
            
        os.makedirs(job_dir, exist_ok=True)
        img_path = os.path.join(job_dir, self.output_filename)
        cv2.imwrite(img_path, canvas_shifted)

        # 5. EXPORTACIÓN AL FRONTEND: Enviamos el universo 4D completo.
        # CRÍTICO: Casteamos a float32 para que el Float32Array de JS pueda leerlo.
        raw_data_path = os.path.join(job_dir, "intensidades_raw.npy")
        np.save(raw_data_path, intensidades_abs.astype(np.float32))

        # 6. Registrar metadatos en el contexto y BD
        preview_meta = {
            "max_global": max_global,
            "shift": shift,
            "min_intensity": float(np.min(intensidades_abs)),
            "total_eventos": len(intensidades_abs), # Informamos la población total real
            "mascara_shifted_path": f"{job_id}/{self.output_filename}" if job_id else self.output_filename
        }
        
        context["preview_meta"] = preview_meta
        
        if job_id:
            Job.update_status(
                job_id, 
                engine, 
                status="ESPERANDO_PERCENTIL_USUARIO", 
                progress=18
            )
            logger.info(f"[{self.name}] Pipeline pausado para el job {job_id}. Esperando confirmación de percentil.")

        return context