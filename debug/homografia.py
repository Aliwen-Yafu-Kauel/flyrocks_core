"""
Calcula la matriz H (mina metros -> pixeles) desde correspondencias tiro<->pixel,
replicando EXACTAMENTE el metodo del frontend (getSafeAffineMatrix en
flyRocks_frontend/src/utils/geometry.ts): ajuste AFIN por minimos cuadrados (SVD)
con las coordenadas mundo centradas. Misma convencion que el pipeline
(h_matrix = metros->pixeles; el nodo de categorizacion usa su inversa).

Correspondencias entregadas por el usuario (frame full-res 4K @ 13s):
    313 -> (2411,1217)   502 -> (1664,1269)   801 -> (1687,1014)   716 -> (2391,978)
Validacion: 658 -> (2007,1174).

    uv run python debug/homografia.py
"""
import sys
import csv
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "debug" / "Secuencia (2).csv"
FRAME = ROOT / "debug" / "out" / "4_fase0_referencias" / "frame_full_res_13s.png"
CACHE = ROOT / "debug" / "out" / "_cache"
OUTDIR = ROOT / "debug" / "out" / "4_fase0_referencias"

# correspondencias tiro -> pixel
CORR = {"313": (2411, 1217), "502": (1664, 1269),
        "801": (1687, 1014), "716": (2391, 978)}
CHECK = {"658": (2007, 1174)}


def load_holes():
    holes, times = {}, {}
    for r in list(csv.reader(open(CSV)))[1:]:
        if len(r) >= 5 and r[4].strip().replace('.', '').isdigit():
            holes[r[0].strip()] = (float(r[1]), float(r[2]))
            times[r[0].strip()] = float(r[4])
    return holes, times


def affine_from_correspondences(world, img):
    """Replica getSafeAffineMatrix: LSQ afin con mundo centrado (SVD)."""
    world = np.asarray(world, float)
    img = np.asarray(img, float)
    c = world.mean(0)
    A = np.column_stack([world - c, np.ones(len(world))])   # (n,3)
    X, *_ = np.linalg.lstsq(A, img, rcond=None)             # (3,2)
    a, b, t1l = X[0, 0], X[1, 0], X[2, 0]
    cc, d, t2l = X[0, 1], X[1, 1], X[2, 1]
    t1 = t1l - a * c[0] - b * c[1]
    t2 = t2l - cc * c[0] - d * c[1]
    return np.array([[a, b, t1], [cc, d, t2], [0, 0, 1.0]])


def apply_H(H, pts):
    p = np.column_stack([pts, np.ones(len(pts))])
    q = (H @ p.T).T
    return q[:, :2] / q[:, 2:3]


def main():
    holes, times = load_holes()
    world = [holes[k] for k in CORR]
    img = [CORR[k] for k in CORR]
    H = affine_from_correspondences(world, img)

    print("H (metros -> pixeles):")
    print(np.array2string(H, precision=6, suppress_small=True))
    print("\nh_matrix (para el pipeline / index.html):")
    print("[" + ", ".join("[" + ", ".join(f"{v:.8g}" for v in row) + "]"
                          for row in H) + "]")

    # validacion
    print("\nValidacion:")
    for k, (px, py) in {**CORR, **CHECK}.items():
        pred = apply_H(H, [holes[k]])[0]
        err = np.hypot(pred[0] - px, pred[1] - py)
        tag = "ANCLA " if k in CORR else "CHECK "
        print(f"  {tag}{k}: dado ({px},{py})  pred ({pred[0]:.0f},{pred[1]:.0f})"
              f"  error {err:.1f} px")

    np.savez(CACHE / "homografia.npz", H=H)

    # --- validacion visual: malla proyectada sobre el frame ---
    frame = cv2.imread(str(FRAME))
    allw = np.array([holes[k] for k in holes])
    proj = apply_H(H, allw)
    tt = np.array([times[k] for k in holes])
    tn = ((tt - tt.min()) / (np.ptp(tt) + 1e-6) * 255).astype(np.uint8)
    for (x, y), c in zip(proj, tn):
        col = cv2.applyColorMap(np.uint8([[c]]), cv2.COLORMAP_JET)[0, 0]
        cv2.circle(frame, (int(x), int(y)), 9,
                   (int(col[0]), int(col[1]), int(col[2])), -1)
    for k, (px, py) in CORR.items():
        cv2.drawMarker(frame, (px, py), (255, 255, 255), cv2.MARKER_CROSS, 40, 3)
        cv2.putText(frame, k, (px + 12, py), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                    (255, 255, 255), 3)
    for k, (px, py) in CHECK.items():
        pred = apply_H(H, [holes[k]])[0].astype(int)
        cv2.drawMarker(frame, (px, py), (0, 255, 0), cv2.MARKER_TILTED_CROSS, 40, 3)
        cv2.circle(frame, tuple(pred), 9, (0, 255, 0), 2)
        cv2.line(frame, (px, py), tuple(pred), (0, 255, 0), 2)
    cv2.imwrite(str(OUTDIR / "validacion_homografia.png"), frame)
    print(f"\n[viz] -> {OUTDIR/'validacion_homografia.png'}")


if __name__ == "__main__":
    main()
