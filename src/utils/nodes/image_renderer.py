import cv2
import os

class BackgroundExtractorNode:
    def __init__(self, name="BackgroundExtractor", output_filename="background.jpg"):
        self.name = name
        self.output_filename = output_filename

    def run(self, context):
        video_path = context.get("video_path")
        if not video_path or not os.path.exists(video_path):
            context["error"] = "No se encontró el video."
            return context

        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 2) # Penúltimo frame por seguridad
        ret, frame = cap.read()
        cap.release()

        if ret:
            ruta_salida = os.path.join(os.path.dirname(video_path), self.output_filename)
            cv2.imwrite(ruta_salida, frame)
            context["background_image"] = self.output_filename
        else:
            context["error"] = "Error al extraer el frame."

        return context