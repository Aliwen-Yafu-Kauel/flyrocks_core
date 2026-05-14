import cv2
import numpy as np
import os
from scipy.optimize import linear_sum_assignment

from core.vision import VisionProcessor
from core.tracker import RockTracker
from core.exporter import ResultsExporter

def run_tracking_pipeline(config, job_id: str, progress_callback=None):
    """
    Ejecuta el análisis de video y genera el reporte PDF final integrando el CSV.
    """
    vision = VisionProcessor(config)
    exporter = ResultsExporter(config)

    cap = cv2.VideoCapture(config.VIDEO_PATH)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ret, first_frame = cap.read()
    
    if not ret:
        if progress_callback: progress_callback(0, 0, "Error: No se pudo leer el video", None)
        return

    if progress_callback:
        progress_callback(0, total_frames, "Iniciando procesamiento...", None)
    
    # ... (Toda tu lógica de tracking se mantiene igual) ...
    map_initial = first_frame.copy()
    last_valid_frame = first_frame.copy()
    prev_gray = cv2.cvtColor(first_frame, cv2.COLOR_BGR2GRAY)
    
    trackers, all_paths = [], []
    track_id_count, current_frame = 0, 0

    while True:
        ret, frame = cap.read()
        if not ret: break
        
        current_frame += 1
        last_valid_frame = frame 
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        M = vision.get_fast_stabilization_matrix(prev_gray, curr_gray)
        frame_stab = cv2.warpAffine(frame, M, (frame.shape[1], frame.shape[0])) if M is not None else frame
        gray_stab = cv2.warpAffine(curr_gray, M, (frame.shape[1], frame.shape[0])) if M is not None else curr_gray

        detections = vision.extract_detections(gray_stab)

        assigned_trackers, assigned_detections = set(), set()
        if trackers and detections:
            T_pos = np.array([(t.kalman.statePost[0][0], t.kalman.statePost[1][0]) for t in trackers])
            D_pos = np.array(detections)
            cost = np.linalg.norm(T_pos[:, np.newaxis, :] - D_pos[np.newaxis, :, :], axis=2)
            row, col = linear_sum_assignment(cost)
            
            for r, c in zip(row, col):
                if cost[r, c] < config.DIST_THRESH:
                    trackers[r].update(detections[c])
                    assigned_trackers.add(r)
                    assigned_detections.add(c)

        for i, det in enumerate(detections):
            if i not in assigned_detections:
                # Nota: Asegúrate que config.ORIGIN_ZONE esté en el formato correcto
                if cv2.pointPolygonTest(config.ORIGIN_ZONE, (float(det[0]), float(det[1])), False) >= 0:
                    trackers.append(RockTracker(track_id_count, det, config))
                    track_id_count += 1

        alive = []
        for i, t in enumerate(trackers):
            if i not in assigned_trackers: t.predict_only()
            pos = t.kalman.statePost
            if (t.frames_since_seen <= config.MAX_MISSING and 0 < pos[0][0] < frame.shape[1] and 0 < pos[1][0] < frame.shape[0]):
                alive.append(t)
            elif t.is_confirmed and t.best_valid_path:
                all_paths.append({'path': t.best_valid_path, 'id': t.id})
        trackers = alive
        prev_gray = gray_stab.copy()

        if progress_callback and current_frame % 5 == 0:
            progress_callback(current_frame, total_frames, "Procesando...", None)

    cap.release()

    for t in trackers:
        if t.is_confirmed and t.best_valid_path:
            all_paths.append({'path': t.best_valid_path, 'id': t.id})

    # --- GENERACIÓN DE ARCHIVOS FINALES (MODIFICADO) ---
    if progress_callback:
        progress_callback(total_frames, total_frames, "Generando Reporte PDF con Malla de Pozos...", None)
        
    json_out = f"results_{job_id}.json"
    pdf_out = f"reporte_{job_id}.pdf"
    
    # 1. Exportar datos crudos a JSON
    exporter.export_to_json(all_paths, last_valid_frame.shape, json_out)
    
    # 3. Finalizar proceso
    if progress_callback:
        progress_callback(total_frames, total_frames, "Completado", json_out)