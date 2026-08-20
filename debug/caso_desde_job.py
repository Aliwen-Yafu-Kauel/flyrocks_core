"""
Congela un JOB de la app como CASO offline.

Es el puente entre el paso 3 del wizard y el trabajo de iteracion sobre la
vista. Quien esta probando filtros, la asociacion o el fondo de video no
deberia depender de que Docker este arriba ni de volver a correr el pipeline de
dos minutos por cada idea: baja el analisis una vez y despues itera con
`caso_serve.py`, sin backend.

    uv run python debug/caso_desde_job.py <job_id> [nombre] [--api URL]

Deja `debug/casos/<nombre>/` con la misma forma que produce caso_export.py:

    caso.json      indice con calibracion, zonas, malla y proyecciones
    mascara.png    la mascara de cambios de ESE analisis
    frame.png      el frame de referencia en color
    clip.mp4       el video que consumio el pipeline
    clip_web.mp4   el mismo, para el fondo de video de la vista

Y despues:

    uv run python debug/caso_serve.py
    -> http://localhost:8770/demo/vista.html?caso=<nombre>

Para saber QUE job bajar:  curl http://localhost:8009/api/jobs
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from caso_export import (CASOS, normalizar, escala_px_por_m, convex_hull,
                         generar_clip_web, log)

API = "http://localhost:8009"


def bajar(url, destino):
    """Descarga a disco por trozos: el clip son decenas de MB."""
    import httpx

    with httpx.stream("GET", url, timeout=600) as r:
        if r.status_code != 200:
            log(f"  no esta ({r.status_code}): {url}")
            return False
        with open(destino, "wb") as f:
            for trozo in r.iter_bytes(1 << 20):
                f.write(trozo)
    log(f"  {destino.name}: {destino.stat().st_size / 1e6:.1f} MB")
    return True


def a_pares(z):
    """Las zonas viajan en dos formatos segun que nodo las espere: aplanadas
    dentro de una lista ([[x,y,x,y,...]]) o ya como pares. Se normaliza."""
    if not z:
        return []
    p = z[0]
    if isinstance(p, (list, tuple)) and len(p) == 2 and all(len(q) == 2 for q in z):
        return [[float(q[0]), float(q[1])] for q in z]
    return [[float(p[2 * i]), float(p[2 * i + 1])] for i in range(len(p) // 2)]


def main():
    ap = argparse.ArgumentParser(description="Congela un job de la app como caso")
    ap.add_argument("job")
    ap.add_argument("nombre", nargs="?", help="carpeta del caso (default: job-XXXXXXXX)")
    ap.add_argument("--api", default=API)
    ap.add_argument("--zip", action="store_true",
                    help="comprime el caso para mandarlo por Drive")
    ap.add_argument("--liviano", action="store_true",
                    help="sin el clip original (se queda solo el H.264 de la "
                         "vista): la mitad de peso")
    args = ap.parse_args()

    import httpx

    api = args.api.rstrip("/")
    r = httpx.get(f"{api}/api/results/{args.job}", timeout=120)
    if r.status_code != 200:
        raise SystemExit(f"el core no conoce el job {args.job} ({r.status_code})")
    J = r.json()
    if J.get("is_running"):
        raise SystemExit(f"el analisis todavia corre ({J.get('status')})")

    E = J.get("entrada") or {}
    if not E.get("h_matrix"):
        raise SystemExit("este job es anterior al guardado de la calibracion")

    nombre = args.nombre or f"job-{args.job[:8]}"
    caso_dir = CASOS / nombre
    caso_dir.mkdir(parents=True, exist_ok=True)
    log(f"congelando {args.job[:8]} -> {caso_dir}")

    # --- artefactos
    A = E.get("artefactos") or {}
    base = f"{api}/temp_videos/"
    bajar(base + (A.get("mascara") or "mascara_cambios.png"), caso_dir / "mascara.png")

    # El frame viaja en JPG (pesa 8 veces menos por la red) y el formato de caso
    # lo espera en PNG: se convierte al vuelo en vez de arrastrar dos nombres.
    if A.get("frame"):
        tmp = caso_dir / "_frame.jpg"
        if bajar(base + A["frame"], tmp):
            im = cv2.imread(str(tmp))
            if im is not None:
                cv2.imwrite(str(caso_dir / "frame.png"), im)
            tmp.unlink(missing_ok=True)

    clip = caso_dir / "clip.mp4"
    if A.get("video") and bajar(base + A["video"], clip):
        # El video del wizard ya viene en H.264, asi que sirve tal cual para el
        # fondo de video: se copia en vez de transcodificar. Solo si no lo fuera
        # se paga el ffmpeg (los casos de caso_export.py salen en mp4v y si lo
        # necesitan).
        cap = cv2.VideoCapture(str(clip))
        cc = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((cc >> 8 * i) & 0xFF) for i in range(4))
        ancho = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        alto = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if fourcc.lower() in ("h264", "avc1"):
            shutil.copy2(clip, caso_dir / "clip_web.mp4")
            log(f"  clip_web.mp4: copia directa (ya es {fourcc})")
        else:
            generar_clip_web(clip, caso_dir / "clip_web.mp4")
    else:
        ancho = alto = 0

    # El tamaño del cuadro sale de la mascara si no hubo video.
    if not ancho:
        m = cv2.imread(str(caso_dir / "mascara.png"), cv2.IMREAD_GRAYSCALE)
        alto, ancho = (m.shape[:2] if m is not None else (2160, 3840))

    # --- el indice
    h_matrix = E["h_matrix"]
    escala = escala_px_por_m(h_matrix)
    origen = a_pares(E.get("origin_zone"))
    seguridad = a_pares(E.get("expected_projection_zone"))
    hull = convex_hull(np.array(origen, np.float64)) if origen else np.zeros((0, 2))
    area_px = cv2.contourArea(hull.astype(np.float32)) if len(hull) >= 3 else 0.0
    area_m2 = area_px / (escala * escala) if escala else 0.0

    malla = E.get("malla") or {"meta": {"t_min": 0, "t_max": 1, "frame_inicio": 0,
                                        "fps": 29.97, "A": [[1, 0], [0, 1]], "t": [0, 0]},
                               "pozos": []}
    ancla = (E.get("recorte") or {}).get("ancla_frames")
    if ancla is None:
        ancla = malla["meta"].get("frame_inicio", 0)

    caso = {
        "meta": {
            "nombre": nombre,
            "video": E.get("video", "—"),
            "job": args.job,
            "ancho": ancho, "alto": alto,
            "fps": malla["meta"].get("fps", 29.97),
            "frame_detonacion": ancla,
            "generado": time.strftime("%Y-%m-%d %H:%M"),
        },
        "calibra": {
            "h_matrix": h_matrix,
            "convencion": "metros -> pixeles; el pipeline usa su inversa",
            "rms_px": None,
            "escala_px_por_m": round(escala, 4),
            "nadir": [ancho / 2, alto / 2], "k": 0.0, "sigma_grados": 4.0,
        },
        "pipeline": E.get("parametros", {}),
        "zonas": {
            "origen": origen, "seguridad": seguridad,
            "origen_mundo": None,
            "diametro_equiv_m": round(float(2 * np.sqrt(max(area_m2, 1) / np.pi)), 2),
            "seguridad_m": None,
        },
        "malla": {"meta": malla["meta"], "pozos": malla.get("pozos", [])},
        "proyecciones": normalizar(J.get("json_data") or {}),
        "vista": {},
    }
    destino = caso_dir / "caso.json"
    destino.write_text(json.dumps(caso, ensure_ascii=False), encoding="utf-8")
    log(f"  caso.json: {len(caso['proyecciones'])} trayectorias, "
        f"{len(caso['malla']['pozos'])} pozos, ancla {ancla}")
    # El clip original solo sirve para reprocesar; para MIRAR la vista basta
    # el derivado H.264. Quitarlo deja el caso a la mitad, que es la diferencia
    # entre mandar 150 MB o 60 MB por Drive.
    if args.liviano and clip.exists() and (caso_dir / "clip_web.mp4").exists():
        clip.unlink()
        log("  clip original descartado (--liviano)")

    if args.zip:
        import shutil as _sh
        _sh.make_archive(str(CASOS / nombre), "zip", root_dir=str(CASOS),
                         base_dir=nombre)
        tam = (CASOS / (nombre + ".zip")).stat().st_size / 1e6
        log(f"  {nombre}.zip: {tam:.0f} MB")
        print()
        print("  Para abrirlo en otro equipo: descomprimir en debug/casos/ y")
        print("    uv run python debug/caso_serve.py")
        print(f"    http://localhost:8770/demo/vista.html?caso={nombre}")
        print()
        return

    print(f"\n  listo. Para verlo:\n"
          f"    uv run python debug/caso_serve.py\n"
          f"    http://localhost:8770/demo/vista.html?caso={nombre}\n")


if __name__ == "__main__":
    main()
