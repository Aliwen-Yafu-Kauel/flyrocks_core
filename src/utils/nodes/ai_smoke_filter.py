import numpy as np
import cv2
import logging
import gc
import onnxruntime as ort
import time
from typing import Any, Dict

from .base import PipelineNode

logger = logging.getLogger(__name__)

class AISmokeFilterNode(PipelineNode):
    """
    Proyecta ventanas temporales del tensor de eventos a 2D y utiliza
    una red neuronal ONNX para identificar y eliminar puntos asociados al humo.
    """
    def __init__(
        self, 
        name: str = "AISmokeFilter", 
        onnx_path: str = "detovision_model_v18.onnx", 
        frames_contexto: int = 60, 
        avance_frames: int = 30, 
        umbral_prob: float = 0.90
    ):
        super().__init__(name)
        self.onnx_path = onnx_path
        self.frames_contexto = frames_contexto
        self.avance_frames = avance_frames
        self.umbral_prob = umbral_prob

    def _obtener_probabilidad_humo(self, session, input_name, image_gray):
        img_h, img_w = image_gray.shape
        pad_w = (128 - (img_w % 128)) % 128
        pad_h = (128 - (img_h % 128)) % 128
        
        if pad_h > 0 or pad_w > 0:
            padded = np.pad(image_gray, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=0)
        else:
            padded = image_gray
            
        input_tensor = (padded.astype(np.float32) / 255.0)[np.newaxis, np.newaxis, :, :]
        
        logits = session.run(None, {input_name: input_tensor})[0][0]
        logits_cropped = logits[:, :img_h, :img_w]
        
        # NUESTRA OPTIMIZACIÓN: Matemática In-place para no duplicar matrices pesadas en RAM
        max_logits = np.max(logits_cropped, axis=0, keepdims=True)
        logits_cropped -= max_logits 
        np.exp(logits_cropped, out=logits_cropped)
        sum_exp = np.sum(logits_cropped, axis=0, keepdims=True)
        logits_cropped /= sum_exp 
        
        prob_humo = logits_cropped[1, :, :].copy() 
        return prob_humo

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        if tensor is None or len(tensor) == 0:
            logger.warning(f"[{self.name}] No se encontró 'tensor_raw' o está vacío. Omitiendo.")
            return context

        try:
            # SIN arena de memoria. (Comentario del equipo conservado)
            opciones = ort.SessionOptions()
            opciones.enable_cpu_mem_arena = False
            session = ort.InferenceSession(self.onnx_path, opciones,
                                           providers=['CPUExecutionProvider'])
            input_name = session.get_inputs()[0].name
        except Exception as e:
            context["error"] = f"Error al cargar el modelo ONNX en {self.onnx_path}: {e}"
            return context

        max_x = int(np.max(tensor[:, 0])) + 1
        max_y = int(np.max(tensor[:, 1])) + 1
        max_t = int(np.max(tensor[:, 2])) + 1 
        
        tensor_filtrado = []
        
        ventanas = len(range(0, max_t, self.avance_frames))
        logger.info(
            f"[{self.name}] {len(tensor):,} eventos en {max_t} frames, "
            f"cuadro {max_x}x{max_y}. {ventanas} ventanas de "
            f"{self.frames_contexto} frames cada {self.avance_frames}, "
            f"umbral {self.umbral_prob}")
        t_inicio = time.time()
        n_ventana = 0

        for start_frame in range(0, max_t, self.avance_frames):
            end_frame_ctx = start_frame + self.frames_contexto
            
            puntos_ctx = tensor[(tensor[:, 2] >= start_frame) & (tensor[:, 2] < end_frame_ctx)]
            
            canvas = np.zeros((max_y, max_x), dtype=np.float32)
            if len(puntos_ctx) > 0:
                np.maximum.at(canvas, (puntos_ctx[:, 1].astype(int), puntos_ctx[:, 0].astype(int)), puntos_ctx[:, 3])
            
            img_intensidad = cv2.normalize(canvas, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
            
            # NUESTRA OPTIMIZACIÓN: Borramos el canvas 4K pesado antes de inferir
            del canvas
            
            prob_humo = self._obtener_probabilidad_humo(session, input_name, img_intensidad)
            
            end_frame_guardado = min(start_frame + self.avance_frames, max_t)
            puntos_app = tensor[(tensor[:, 2] >= start_frame) & (tensor[:, 2] < end_frame_guardado)]
            
            # OPTIMIZACIÓN DEL EQUIPO: Vectorización con validación de límites (dentro)
            if len(puntos_app) > 0:
                xs = puntos_app[:, 0].astype(np.intp)
                ys = puntos_app[:, 1].astype(np.intp)
                dentro = (ys < prob_humo.shape[0]) & (xs < prob_humo.shape[1])
                conservar = np.zeros(len(puntos_app), dtype=bool)
                conservar[dentro] = prob_humo[ys[dentro], xs[dentro]] < self.umbral_prob
                if conservar.any():
                    tensor_filtrado.append(puntos_app[conservar])

                vivos = int(conservar.sum())
            else:
                vivos = 0
                
            total_v = len(puntos_app)
            n_ventana += 1
            
            logger.info(
                f"[{self.name}] ventana {n_ventana}/{ventanas} "
                f"(frames {start_frame}-{end_frame_ctx}): "
                f"{total_v:,} pts -> {vivos:,} ({vivos/max(total_v,1)*100:.0f}% "
                f"conservado) | {time.time() - t_inicio:.0f}s acumulados")

            # NUESTRA OPTIMIZACIÓN: Forzamos la limpieza agresiva en Docker
            del img_intensidad, prob_humo, puntos_ctx, puntos_app
            gc.collect()

        tensor_final = (np.concatenate(tensor_filtrado) if tensor_filtrado
                        else np.empty((0, 4)))
        
        retencion = (len(tensor_final) / len(tensor)) * 100 if len(tensor) > 0 else 0
        logger.info(
            f"[{self.name}] LISTO en {time.time() - t_inicio:.0f}s: "
            f"{len(tensor):,} -> {len(tensor_final):,} pts "
            f"({retencion:.1f}% conservado, {100-retencion:.1f}% descartado "
            f"como humo)")
        
        context["tensor_raw"] = tensor_final
        
        del session
        gc.collect()
        
        return context