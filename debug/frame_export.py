"""
Extrae un frame del video de cliente por su indice EN EL CLIP de tronadura.

El pipeline entero trabaja sobre `clip_full.mp4` (ventana 12.5-27.6 s del video
original, 453 frames @ 29.97). Ese clip vive en un scratchpad temporal y se
borra; el video original si esta versionado. Este script hace la conversion
para no tener que acordarse del offset:

    tiempo_original = 12.5 s + frame_clip / 29.97

Frames de referencia:
    48  -> detonacion del primer tiro
    69  -> ancla de la homografia (h_matrix.json)

El uso tipico es sacar un frame ANTES del 48: el terreno seco, sin humo ni
estelas, es donde mejor se juzga si la malla de tiros calza.

    uv run python debug/frame_export.py [frame_clip ...]     (por defecto 45)
"""
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "debug" / "Video de cliente 3160-789.mp4"
OUT = ROOT / "debug" / "out" / "4_fase0_referencias"

CLIP_INICIO_S = 12.5      # mask_combined.py: "ventana completa (12.5-27.6s)"
FPS = 29.97
START = 48                # frame de detonacion dentro del clip


def main():
    if not VIDEO.exists():
        raise SystemExit(f"falta {VIDEO}")

    frames = [int(a) for a in sys.argv[1:]] or [45]
    cap = cv2.VideoCapture(str(VIDEO))
    fps_real = cap.get(cv2.CAP_PROP_FPS)
    OUT.mkdir(parents=True, exist_ok=True)

    for f in frames:
        t = CLIP_INICIO_S + f / FPS
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, img = cap.read()
        if not ok:
            print(f"  frame {f}: NO se pudo leer en t={t:.3f}s")
            continue
        rel = "pre-tronadura" if f < START else f"+{f - START} frames"
        dst = OUT / f"frame_clip_{f:03d}.png"
        cv2.imwrite(str(dst), img)
        print(f"  frame {f:3d}  t={t:.3f}s del original  {img.shape[1]}x{img.shape[0]}"
              f"  [{rel}]  -> {dst.name}")

    cap.release()
    print(f"\n  (video original a {fps_real:.2f} fps; clip a {FPS} fps desde "
          f"{CLIP_INICIO_S}s, detonacion en frame {START})")


if __name__ == "__main__":
    main()
