"""
Congela un CASO completo: corre el pipeline del core una vez y empaqueta todo
lo que la vista necesita en `debug/casos/<nombre>/`.

El punto de esto es no volver a correr el pipeline nunca mas mientras iteramos
la vista. Mismo patron que ya usamos con `h_matrix.json`, pero para el estado
entero en vez de un solo artefacto.

    uv run python debug/caso_export.py [nombre_del_caso]

Es IDEMPOTENTE por etapas: si el clip ya existe no lo regenera, si el JSON de
resultados ya existe no vuelve a correr el pipeline. Para rehacer una etapa,
borra su archivo. Para rehacer todo, borra la carpeta del caso.

Lo que produce:

    debug/casos/<nombre>/
        caso.json      indice con calibracion, zonas, malla y proyecciones
        clip.mp4       el recorte que consumio el pipeline
        clip_web.mp4   el mismo recorte en H.264, para el fondo de video
        frame.png      frame de referencia (terreno seco, pre-tronadura)
        crudo.json     salida tal cual del pipeline, sin normalizar

De donde sale cada cosa:
  - h_matrix       -> debug/out/7_preproceso/05_tiros/h_matrix.json (RMS 3.19 px)
  - malla          -> debug/demo/malla.json (113 pozos ya proyectados)
  - zonas          -> DERIVADAS de la malla, no dibujadas a mano (ver abajo)
  - proyecciones   -> el pipeline

Las zonas se derivan en vez de dibujarse porque asi el caso es reproducible:
`origin_zone` es el convex hull de los pozos (que es, literalmente, el area de
la voladura) y `expected_projection_zone` es ese hull dilatado N metros. El
valor de N define el reparto Proyeccion / Proyeccion peligrosa, asi que queda
registrado en el caso.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Los nodos del core imprimen emoji en sus logs. La consola de Windows es
# cp1252 y eso revienta el pipeline entero con UnicodeEncodeError a mitad de
# camino (paso en el nodo 12). No es un bug del algoritmo, pero mata la corrida.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VIDEO = ROOT / "debug" / "Video de cliente 3160-789.mp4"
H_MATRIX = ROOT / "debug" / "out" / "7_preproceso" / "05_tiros" / "h_matrix.json"
MALLA = ROOT / "debug" / "demo" / "malla.json"
CASOS = ROOT / "debug" / "casos"

# Ventana del clip. Misma que usa todo el pipeline de debug (ver frame_export.py).
CLIP_INICIO_S = 12.5
CLIP_FIN_S = 27.6
FRAME_REFERENCIA = 45          # pre-tronadura: terreno seco, sin humo

# Parametros del pipeline. Son los defaults que el front trae en Step3.
# Se pueden pisar por entorno para iterar sin editar el archivo: la cache del
# pipeline invalida solo del nodo afectado en adelante, asi que probar un
# `SIGMA` distinto cuesta segundos y no los ~110 s de la corrida completa.
#     SIGMA=0.7 uv run python debug/caso_export.py
PERCENTILE = float(os.getenv("PERCENTILE", "96.0"))
SIGMA = float(os.getenv("SIGMA", "0.5"))
ESP = float(os.getenv("ESP", "5.0"))

# Margen del area de seguridad, en metros desde el borde de la voladura.
# 100 m es el default de `polygonDistance` en el frontend (Step3.tsx:78, con
# tope 300), que ademas viaja al generador de PDF como `radio_equipos`. Se usa
# el mismo valor para que la zona que ve el cliente en las dos vistas coincida.
SEGURIDAD_M = 100.0


def log(msg):
    print(f"  {msg}", flush=True)


# --------------------------------------------------------------- geometria

def escala_px_por_m(h_matrix):
    """px/m de la homografia metros->pixeles: raiz del |det| del bloque lineal."""
    a = np.array(h_matrix, dtype=np.float64)[:2, :2]
    return float(np.sqrt(abs(np.linalg.det(a))))


def convex_hull(puntos_px):
    pts = np.array(puntos_px, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.convexHull(pts).reshape(-1, 2)


def offset_redondeado(poly, distancia, arc_segments=5):
    """Offset de un poligono CONVEXO por un disco, con arcos reales.

    Es la misma construccion que `offsetConvexPolygonRounded` del frontend
    (`src/utils/geometry.ts`), con su mismo `arcSegments=5`, para que la zona
    que dibuja el colega y la que reproducimos aca sean identicas.

    Va en METROS, igual que alla: el front hace el hull y el offset sobre las
    coordenadas del CSV y recien despues proyecta a pixeles. Importa porque un
    disco en metros no tiene por que ser un disco en pixeles si la homografia
    no es isotropica.
    """
    P = np.asarray(poly, dtype=np.float64)
    if len(P) < 3 or distancia == 0:
        return P

    # Orden angular respecto al centroide: deja el contorno en sentido unico
    # sin depender de como venian los vertices.
    c = P.mean(axis=0)
    P = P[np.argsort(np.arctan2(P[:, 1] - c[1], P[:, 0] - c[0]))]
    n = len(P)

    normales = []
    for i in range(n):
        a, b = P[i], P[(i + 1) % n]
        d = b - a
        L = np.hypot(*d) or 1.0
        nv = np.array([d[1], -d[0]]) / L
        if np.dot(nv, (a + b) / 2 - c) < 0:      # que apunte hacia afuera
            nv = -nv
        normales.append(nv)

    salida = []
    for i in range(n):
        v = P[i]
        n_prev, n_next = normales[(i - 1) % n], normales[i]
        a0 = np.arctan2(*n_prev[::-1])
        a1 = np.arctan2(*n_next[::-1])
        d = (a1 - a0 + np.pi) % (2 * np.pi) - np.pi
        for s in range(arc_segments + 1):
            a = a0 + d * s / arc_segments
            salida.append(v + distancia * np.array([np.cos(a), np.sin(a)]))
    return np.array(salida)


def mundo_a_px(pts, meta):
    """Coordenadas de malla (metros, relativas al centro) -> pixeles."""
    A = np.array(meta["A"], dtype=np.float64)
    t = np.array(meta["t"], dtype=np.float64)
    return np.asarray(pts, dtype=np.float64) @ A.T + t


# ------------------------------------------------------------------ etapas

def generar_clip(destino):
    if destino.exists():
        log(f"clip ya existe, lo reuso ({destino.stat().st_size / 1e6:.0f} MB)")
        return
    if not VIDEO.exists():
        raise SystemExit(f"falta el video original: {VIDEO}")

    log(f"cortando {CLIP_INICIO_S}-{CLIP_FIN_S}s del video original...")
    cap = cv2.VideoCapture(str(VIDEO))
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_MSEC, CLIP_INICIO_S * 1000)

    out = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not out.isOpened():
        raise SystemExit("no se pudo abrir el VideoWriter (codec mp4v)")

    n, limite = 0, (CLIP_FIN_S - CLIP_INICIO_S) * 1000
    t0 = time.time()
    while True:
        pos = cap.get(cv2.CAP_PROP_POS_MSEC) - CLIP_INICIO_S * 1000
        ok, frame = cap.read()
        if not ok or pos > limite:
            break
        out.write(frame)
        n += 1
        if n % 100 == 0:
            log(f"    {n} frames...")
    out.release()
    cap.release()
    log(f"clip: {n} frames @ {fps:.2f} fps, {w}x{h}, "
        f"{destino.stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f}s")


def generar_clip_web(origen, destino):
    """El mismo clip, pero en un codec que el navegador sepa reproducir.

    `generar_clip` escribe con el fourcc `mp4v` de OpenCV, que es MPEG-4 parte
    2: el pipeline lo lee sin problema, pero ningun navegador lo reproduce (se
    ve negro y sin error claro). La vista necesita H.264, asi que se deriva una
    copia y se deja al lado. No se reemplaza el original: el pipeline consume
    ese y no hay razon para tocarle el codec.

    Los keyframes van cada 15 cuadros (`-g 15`) a proposito: con el GOP largo
    por defecto, avanzar de a un frame obliga al navegador a decodificar desde
    el keyframe anterior y el salto se siente pegajoso. Cuesta algo de tamano y
    lo vale, porque frame a frame es justo para lo que se usa.
    """
    if destino.exists():
        log(f"clip web ya existe, lo reuso ({destino.stat().st_size / 1e6:.0f} MB)")
        return
    if not origen.exists():
        return

    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        log("OJO: no hay ffmpeg en el PATH: la vista se queda sin fondo de video")
        return

    log("convirtiendo el clip a H.264 para la vista...")
    t0 = time.time()
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(origen),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
         "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
         str(destino)],
        capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ffmpeg fallo: {r.stderr.strip()[:300]}")
        destino.unlink(missing_ok=True)
        return
    log(f"clip web: {destino.stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f}s")


def exportar_frame(destino):
    if destino.exists():
        return
    cap = cv2.VideoCapture(str(VIDEO))
    cap.set(cv2.CAP_PROP_POS_MSEC, (CLIP_INICIO_S + FRAME_REFERENCIA / 29.97) * 1000)
    ok, img = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(str(destino), img)
        log(f"frame de referencia {FRAME_REFERENCIA} (pre-tronadura) -> {destino.name}")


def correr_pipeline(clip, zonas, h_matrix, destino):
    if destino.exists():
        log(f"resultados ya existen, no vuelvo a correr el pipeline "
            f"({destino.stat().st_size / 1e3:.0f} KB)")
        return json.loads(destino.read_text(encoding="utf-8"))

    from sqlmodel import Session, SQLModel
    from utils.database import Job, engine
    from utils.services import run_pipeline_task

    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        job = Job(status="caso_export", progress=0)
        s.add(job)
        s.commit()
        s.refresh(job)
        job_id = job.id

    log(f"corriendo el pipeline (job {job_id[:8]})... 13 nodos, esto demora")
    t0 = time.time()
    run_pipeline_task(
        job_id,
        str(clip),
        zonas["origin_zone"],
        zonas["expected_projection_zone"],
        h_matrix,
        PERCENTILE,
        SIGMA,
        ESP,
        output_filename="caso_resultados.json",
    )
    log(f"pipeline: {time.time() - t0:.0f}s")

    with Session(engine) as s:
        job = s.get(Job, job_id)
        if job.error_message:
            raise SystemExit(f"el pipeline fallo: {job.error_message}")
        resultados = job.json_data or {}

    if not resultados:
        raise SystemExit("el pipeline no devolvio trayectorias")

    destino.write_text(json.dumps(resultados, indent=1, ensure_ascii=False),
                       encoding="utf-8")
    return resultados


# ------------------------------------------------------------ normalizacion

def normalizar(crudo):
    """Del dict del pipeline (indexado por track_id) a una lista con estado.

    El modelo de estado es deliberado: los filtros NO borran, marcan. Asi la
    vista siempre puede responder por que una trayectoria no esta en pantalla,
    que hoy es imposible con 3 sliders + checkboxes + capa de borrados.
    """
    salida = []
    for track_id, d in crudo.items():
        pts = d.get("puntos") or []
        fr = d.get("frames") or []
        salida.append({
            "id": str(track_id),
            "fuente": "pipeline",
            "puntos": pts,                       # [x,y,x,y,...] traza cruda
            # Frame de cada punto, ascendente. Es lo que habilita el calce
            # temporal: sin esto la asociacion solo puede mirar geometria.
            "frames": fr,
            "t_ini": fr[0] if fr else None,
            "t_fin": fr[-1] if fr else None,
            "clasificacion": d.get("clasificacion"),
            "distancia_m": d.get("distancia_m"),
            "tortuosidad": d.get("tortuosidad"),
            "escape_relativo": d.get("escape_relativo"),
            "r2_score": d.get("r2_score"),
            "estado": "activa",
            "razon": None,
            "asociacion": None,                  # lo llena la fase geometrica
        })
    return salida


# -------------------------------------------------------------------- main

def main():
    nombre = sys.argv[1] if len(sys.argv) > 1 else "3160-789"
    caso_dir = CASOS / nombre
    caso_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== CASO: {nombre}  ->  {caso_dir.relative_to(ROOT)}\n")

    # --- insumos ya calibrados
    for f in (H_MATRIX, MALLA):
        if not f.exists():
            raise SystemExit(f"falta {f}")
    hm = json.loads(H_MATRIX.read_text(encoding="utf-8"))
    malla = json.loads(MALLA.read_text(encoding="utf-8"))
    h_matrix = hm["h_matrix"]
    pozos = malla["pozos"]
    ancho, alto = malla["meta"]["ancho"], malla["meta"]["alto"]
    escala = escala_px_por_m(h_matrix)
    log(f"h_matrix RMS {hm['rms_px']} px | escala {escala:.2f} px/m | "
        f"{len(pozos)} pozos | cuadro {ancho}x{alto}")

    # --- zonas derivadas de la malla, EN METROS y luego proyectadas (igual
    #     que el frontend, para que las dos vistas dibujen lo mismo)
    hull_m = convex_hull([(p["x"], p["y"]) for p in pozos])
    seguridad_m = offset_redondeado(hull_m, SEGURIDAD_M)
    hull = mundo_a_px(hull_m, malla["meta"])
    seguridad = mundo_a_px(seguridad_m, malla["meta"])

    area_m2 = cv2.contourArea(np.asarray(hull_m, np.float32).reshape(-1, 1, 2))
    log(f"zona de origen: {len(hull)} vertices, {area_m2:,.0f} m2 "
        f"(diametro equiv. {2 * np.sqrt(area_m2 / np.pi):.0f} m)")
    log(f"zona de seguridad: hull + {SEGURIDAD_M:.0f} m con arcos "
        f"-> {len(seguridad)} vertices")

    zonas = {
        # OJO: formatos distintos, asi los espera cada nodo del core.
        "origin_zone": [hull.flatten().tolist()],            # [[x,y,x,y,...]]
        "expected_projection_zone": seguridad.tolist(),      # [[x,y],...]
    }

    # --- etapas
    clip = caso_dir / "clip.mp4"
    generar_clip(clip)
    generar_clip_web(clip, caso_dir / "clip_web.mp4")
    exportar_frame(caso_dir / "frame.png")
    crudo = correr_pipeline(clip, zonas, h_matrix, caso_dir / "crudo.json")

    proyecciones = normalizar(crudo)
    log(f"{len(proyecciones)} trayectorias")
    for c in ("Proyección", "Proyección peligrosa", "Fuera de vista"):
        n = sum(1 for p in proyecciones if p["clasificacion"] == c)
        if n:
            log(f"    {c}: {n}")

    # El EventExtractor deja la mascara de cambios junto al video de entrada,
    # o sea ya cae dentro de la carpeta del caso. Solo la renombramos.
    suelta = caso_dir / "mascara_cambios.png"
    if suelta.exists():
        suelta.replace(caso_dir / "mascara.png")
        log(f"mascara de cambios -> mascara.png "
            f"({(caso_dir / 'mascara.png').stat().st_size / 1e6:.1f} MB)")

    # --- el indice
    caso = {
        "meta": {
            "nombre": nombre,
            "video": VIDEO.name,
            "clip_s": [CLIP_INICIO_S, CLIP_FIN_S],
            "frame_referencia": FRAME_REFERENCIA,
            "ancho": ancho,
            "alto": alto,
            "fps": malla["meta"]["fps"],
            "frame_detonacion": malla["meta"]["frame_inicio"],
            "generado": time.strftime("%Y-%m-%d %H:%M"),
        },
        "calibra": {
            "h_matrix": h_matrix,
            "convencion": "metros -> pixeles; el pipeline usa su inversa",
            "rms_px": hm["rms_px"],
            "escala_px_por_m": round(escala, 4),
            # Parametros de la asociacion geometrica. El nadir arranca en el
            # centro del cuadro y se ajusta a mano; k=0 deja el paralaje
            # apagado, que es el default deliberado hasta calibrarlo con datos
            # reales (ver PLAN_ASOCIACION.md).
            "nadir": [ancho / 2, alto / 2],
            "k": 0.0,
            "sigma_grados": 4.0,
        },
        "pipeline": {"percentile": PERCENTILE, "sigma": SIGMA, "esp": ESP},
        "zonas": {
            "origen": hull.tolist(),
            "seguridad": seguridad.tolist(),
            "seguridad_m": SEGURIDAD_M,
            # El hull en METROS viaja tambien: la vista lo necesita para dibujar
            # isolineas a distancias reales sin tener que deshacer la proyeccion.
            "origen_mundo": np.asarray(hull_m).tolist(),
            "diametro_equiv_m": round(float(2 * np.sqrt(area_m2 / np.pi)), 2),
        },
        "malla": {"meta": malla["meta"], "pozos": pozos},
        "proyecciones": proyecciones,
        "vista": {"tortuosidad": 5.0, "escape_relativo": 0.0, "r2_score": 0.0},
    }
    destino = caso_dir / "caso.json"
    destino.write_text(json.dumps(caso, ensure_ascii=False), encoding="utf-8")
    log(f"caso.json -> {destino.stat().st_size / 1e6:.1f} MB")
    print(f"\n=== listo. la vista ya no necesita el pipeline.\n")


if __name__ == "__main__":
    main()
