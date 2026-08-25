import numpy as np
import cv2
import logging
import math
import math
import onnxruntime as ort
from typing import Any, Dict

from .base import PipelineNode

logger = logging.getLogger(__name__)

class AISmokeFilterNode(PipelineNode):
    """
    Proyecta ventanas temporales del tensor de eventos a 2D y utiliza
    una red neuronal ONNX para identificar y eliminar puntos asociados al humo.
    Incluye optimización de rendimiento mediante reducción de resolución con Max-Pooling.
    Incluye optimización de rendimiento mediante reducción de resolución con Max-Pooling.
    """
    def __init__(
        self, 
        name: str = "AISmokeFilter", 
        onnx_path: str = "detovision_model_v18.onnx", 
        frames_contexto: int = 60, 
        avance_frames: int = 30, 
        umbral_prob: float = 0.90,
        escala: float = 0.75  # <-- Integración del parámetro de escala
    ):
        super().__init__(name)
        self.onnx_path = onnx_path
        self.frames_contexto = frames_contexto
        self.avance_frames = avance_frames
        self.umbral_prob = umbral_prob
        self.escala = escala
        self.escala = escala

    def _obtener_probabilidad_humo(self, session, input_name, image_gray):
        h_orig, w_orig = image_gray.shape
        
        # 1. Reducción con Max Pooling simulado (Dilatación + Nearest)
        w_nuevo, h_nuevo = int(w_orig * self.escala), int(h_orig * self.escala)
        
        factor = math.ceil(1.0 / self.escala) 
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (factor, factor))
        
        img_dilatada = cv2.dilate(image_gray, kernel)
        img_reducida = cv2.resize(img_dilatada, (w_nuevo, h_nuevo), interpolation=cv2.INTER_NEAREST)
            
        img_h, img_w = img_reducida.shape
        
        # 2. Padding a múltiplos de 128 para la IA
        h_orig, w_orig = image_gray.shape
        
        # 1. Reducción con Max Pooling simulado (Dilatación + Nearest)
        w_nuevo, h_nuevo = int(w_orig * self.escala), int(h_orig * self.escala)
        
        factor = math.ceil(1.0 / self.escala) 
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (factor, factor))
        
        img_dilatada = cv2.dilate(image_gray, kernel)
        img_reducida = cv2.resize(img_dilatada, (w_nuevo, h_nuevo), interpolation=cv2.INTER_NEAREST)
            
        img_h, img_w = img_reducida.shape
        
        # 2. Padding a múltiplos de 128 para la IA
        pad_w = (128 - (img_w % 128)) % 128
        pad_h = (128 - (img_h % 128)) % 128
        
        if pad_h > 0 or pad_w > 0:
            padded = np.pad(img_reducida, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=0)
            padded = np.pad(img_reducida, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=0)
        else:
            padded = img_reducida
            padded = img_reducida
            
        # 3. Inferencia
        # 3. Inferencia
        input_tensor = (padded.astype(np.float32) / 255.0)[np.newaxis, np.newaxis, :, :]
        
        logits = session.run(None, {input_name: input_tensor})[0][0]
        logits_cropped = logits[:, :img_h, :img_w]
        
        # 4. Softmax
        exp_logits = np.exp(logits_cropped - np.max(logits_cropped, axis=0, keepdims=True))
        probabilidades = exp_logits / np.sum(exp_logits, axis=0, keepdims=True)
        prob_humo_reducida = probabilidades[1, :, :] # Retorna la capa de la clase 1 (Humo)
        
        # 5. Restaurar a resolución original
        prob_humo_restaurada = cv2.resize(prob_humo_reducida, (w_orig, h_orig), interpolation=cv2.INTER_LINEAR)
        
        return prob_humo_restaurada
        # 4. Softmax
        exp_logits = np.exp(logits_cropped - np.max(logits_cropped, axis=0, keepdims=True))
        probabilidades = exp_logits / np.sum(exp_logits, axis=0, keepdims=True)
        prob_humo_reducida = probabilidades[1, :, :] # Retorna la capa de la clase 1 (Humo)
        
        # 5. Restaurar a resolución original
        prob_humo_restaurada = cv2.resize(prob_humo_reducida, (w_orig, h_orig), interpolation=cv2.INTER_LINEAR)
        
        return prob_humo_restaurada

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        tensor = context.get("tensor_raw")
        if tensor is None or len(tensor) == 0:
            logger.warning(f"[{self.name}] No se encontró 'tensor_raw' o está vacío. Omitiendo.")
            return context

        try:
            session = ort.InferenceSession(self.onnx_path, providers=['CPUExecutionProvider'])
            session = ort.InferenceSession(self.onnx_path, providers=['CPUExecutionProvider'])
            input_name = session.get_inputs()[0].name
        except Exception as e:
            context["error"] = f"Error al cargar el modelo ONNX en {self.onnx_path}: {e}"
            return context

        max_x = int(np.max(tensor[:, 0])) + 1
        max_y = int(np.max(tensor[:, 1])) + 1
        max_t = int(np.max(tensor[:, 2])) + 1 
        
        tensor_filtrado = []
        
        logger.info(f"[{self.name}] Ejecutando inferencia en ventanas temporales (Escala: {self.escala*100:.0f}%)...")
        logger.info(f"[{self.name}] Ejecutando inferencia en ventanas temporales (Escala: {self.escala*100:.0f}%)...")

        for start_frame in range(0, max_t, self.avance_frames):
            end_frame_ctx = start_frame + self.frames_contexto
            
            puntos_ctx = tensor[(tensor[:, 2] >= start_frame) & (tensor[:, 2] < end_frame_ctx)]
            
            canvas = np.zeros((max_y, max_x), dtype=np.float32)
            if len(puntos_ctx) > 0:
                np.maximum.at(canvas, (puntos_ctx[:, 1].astype(int), puntos_ctx[:, 0].astype(int)), puntos_ctx[:, 3])
            
            img_intensidad = cv2.normalize(canvas, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
            
            # Usando el nuevo motor con Max-Pooling
            # Usando el nuevo motor con Max-Pooling
            prob_humo = self._obtener_probabilidad_humo(session, input_name, img_intensidad)
            
            end_frame_guardado = min(start_frame + self.avance_frames, max_t)
            puntos_app = tensor[(tensor[:, 2] >= start_frame) & (tensor[:, 2] < end_frame_guardado)]
            
            puntos_validos = []
            for p in puntos_app:
                x, y = int(p[0]), int(p[1])
                # Ya no es necesario escalar las coordenadas X e Y porque prob_humo fue restaurada a su tamaño original
                if y < prob_humo.shape[0] and x < prob_humo.shape[1]:
                    if prob_humo[y, x] < self.umbral_prob:  
                        puntos_validos.append(p)
                        
            tensor_filtrado.extend(puntos_validos)
            
        tensor_final = np.array(tensor_filtrado) if tensor_filtrado else np.empty((0, 4))
            puntos_validos = []
            for p in puntos_app:
                x, y = int(p[0]), int(p[1])
                # Ya no es necesario escalar las coordenadas X e Y porque prob_humo fue restaurada a su tamaño original
                if y < prob_humo.shape[0] and x < prob_humo.shape[1]:
                    if prob_humo[y, x] < self.umbral_prob:  
                        puntos_validos.append(p)
                        
            tensor_filtrado.extend(puntos_validos)
            
        tensor_final = np.array(tensor_filtrado) if tensor_filtrado else np.empty((0, 4))
        
        retencion = (len(tensor_final) / len(tensor)) * 100 if len(tensor) > 0 else 0
        logger.info(f"[{self.name}] Filtrado completado. Se conservaron {len(tensor_final)} pts ({retencion:.1f}%).")
        logger.info(f"[{self.name}] Filtrado completado. Se conservaron {len(tensor_final)} pts ({retencion:.1f}%).")
        
        context["tensor_raw"] = tensor_final
        return context
