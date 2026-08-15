"""
Exporta la malla de tiros ya proyectada a pixeles, para el visor de trayectorias.

Toma la homografia YA CALIBRADA por pre_tiros.py (05_tiros/h_matrix.json,
RMS 3.19 px, anclada al frame_69) y el CSV de secuencia, y escribe los 113 pozos
con su posicion en pixeles y su tiempo de detonacion.

No recalibra nada: el ajuste manual ya se hizo una vez y no hay que repetirlo.

Salidas:
  demo/malla.json            malla suelta (por si se quiere cargar a mano)
  demo/trayectorias.html     misma malla INYECTADA entre marcadores, para que el
                             visor funcione con doble clic (file:// bloquea fetch)

Las coordenadas de mundo se guardan CENTRADAS en el centroide de la malla: los
valores originales son de mina (~100669, ~99798) y en float32 de JS pierden
precision util. El centroide va aparte, en meta.

    uv run python debug/malla_export.py
"""
import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "debug" / "Secuencia (2).csv"
H_JSON = ROOT / "debug" / "out" / "7_preproceso" / "05_tiros" / "h_matrix.json"
DEMO = ROOT / "debug" / "demo"
HTML = DEMO / "trayectorias.html"

INI = "/* <<<MALLA_INICIO>>> */"
FIN = "/* <<<MALLA_FIN>>> */"

FPS = 29.97
FRAME_INICIO = 48        # frame donde detona el primer tiro (t = T_MIN)


def cargar_pozos():
    """Lee el CSV. Misma condicion de fila valida que pre_capas.py / homografia.py."""
    pozos = []
    for r in list(csv.reader(open(CSV)))[1:]:
        if len(r) >= 5 and r[4].strip().replace('.', '').isdigit():
            pozos.append({
                "id": r[0].strip(),
                "x": float(r[1]), "y": float(r[2]), "z": float(r[3]),
                "t": float(r[4]),
            })
    return pozos


def main():
    if not H_JSON.exists():
        raise SystemExit(f"falta {H_JSON} — correr antes pre_tiros.py")

    hj = json.loads(H_JSON.read_text(encoding="utf-8"))
    H = np.array(hj["h_matrix"], float)
    pozos = cargar_pozos()

    W = np.array([[p["x"], p["y"]] for p in pozos])
    T = np.array([p["t"] for p in pozos])
    centro = W.mean(0)

    # mundo -> pixel con la H tal cual (misma cuenta que pre_capas.py:203-210)
    hom = np.column_stack([W, np.ones(len(W))])
    q = (H @ hom.T).T
    px = q[:, :2] / q[:, 2:3]

    # La H opera sobre mundo ABSOLUTO. Como en el JS guardamos mundo CENTRADO,
    # reabsorbemos el centroide en la traslacion: A es la parte lineal (identica)
    # y t2 el termino independiente ya evaluado en el centro. Asi el visor puede
    # recalcular la proyeccion si el usuario ajusta la malla.
    A = H[:2, :2]
    t2 = H[:2, :2] @ centro + H[:2, 2]

    err = np.abs(np.column_stack([W - centro, np.ones(len(W))]) @
                 np.vstack([A.T, t2]) - px).max()
    if err > 1e-6:
        raise SystemExit(f"reparametrizacion inconsistente: {err:.2e} px")

    malla = {
        "meta": {
            "fuente_h": str(H_JSON.relative_to(ROOT)).replace("\\", "/"),
            "rms_px": hj.get("rms_px"),
            "imagen_base": hj.get("imagen_base"),
            "ancho": 3840, "alto": 2160,
            "fps": FPS, "frame_inicio": FRAME_INICIO,
            "t_min": float(T.min()), "t_max": float(T.max()),
            "centro_mundo": [float(centro[0]), float(centro[1])],
            # px = A @ (mundo - centro) + t2
            "A": [[float(v) for v in row] for row in A],
            "t": [float(v) for v in t2],
        },
        # x,y = mundo centrado en metros | px,py = pixel | t = ms | z = cota
        "pozos": [
            {"id": p["id"],
             "x": round(float(W[i, 0] - centro[0]), 3),
             "y": round(float(W[i, 1] - centro[1]), 3),
             "z": p["z"],
             "px": round(float(px[i, 0]), 1),
             "py": round(float(px[i, 1]), 1),
             "t": p["t"]}
            for i, p in enumerate(pozos)
        ],
    }

    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "malla.json").write_text(json.dumps(malla, indent=1), encoding="utf-8")

    # --- inyeccion en el HTML ---
    src = HTML.read_text(encoding="utf-8")
    if INI not in src or FIN not in src:
        raise SystemExit(f"faltan los marcadores {INI} / {FIN} en {HTML.name}")
    a = src.index(INI) + len(INI)
    b = src.index(FIN)
    bloque = "\nconst MALLA = " + json.dumps(malla, separators=(",", ":")) + ";\n"
    HTML.write_text(src[:a] + bloque + src[b:], encoding="utf-8")

    d = np.linalg.norm(px[:, None, :] - px[None, :, :], axis=2)
    np.fill_diagonal(d, 1e9)
    print(f"  pozos            {len(pozos)}")
    print(f"  homografia       {hj.get('imagen_base')}  RMS {hj.get('rms_px')} px")
    print(f"  extension px     x {px[:,0].min():.0f}-{px[:,0].max():.0f}   "
          f"y {px[:,1].min():.0f}-{px[:,1].max():.0f}")
    print(f"  vecino cercano   {np.median(d.min(1)):.0f} px (mediana)")
    print(f"  detonacion       {T.min():.0f}-{T.max():.0f} ms  "
          f"= frames {FRAME_INICIO}-{FRAME_INICIO + (T.max()-T.min())/1000*FPS:.0f}")
    print(f"  -> {DEMO/'malla.json'}")
    print(f"  -> inyectada en {HTML.name}")


if __name__ == "__main__":
    main()
