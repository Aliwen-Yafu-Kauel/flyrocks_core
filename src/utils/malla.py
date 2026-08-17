"""Malla de tiros: lee el CSV de secuencia y lo proyecta a pixeles.

Por que vive aca y no en el navegador
-------------------------------------
El CSV de secuencia lo carga el usuario en el paso 3 del wizard. Hasta ahora se
parseaba en el navegador y se quedaba ahi: terminado el analisis, el `job` no
sabia nada de la malla. Sin malla no hay asociacion trayectoria -> tiro, que es
justo lo que el cliente pidio. Con el CSV guardado en el job, cualquier vista
puede reconstruir la malla desde un `job_id`, sin depender de que el navegador
todavia tenga el archivo abierto.

El core ya recibe la homografia en la misma peticion, asi que proyectar es una
multiplicacion de matrices y no agrega ninguna dependencia nueva.

Formato del CSV (el mismo que ya usa el frontend):

    Label, X, Y, Z, DetonatingTime
    101, 100669.14, 99798.78, 3175.89, 5349

X e Y son coordenadas de mina en metros; DetonatingTime va en milisegundos.
"""
import csv
import io
from typing import Optional

import numpy as np

# Nombres de columna que reconocemos, en el orden en que los devolvemos.
# Se compara en minusculas y sin espacios, porque el archivo viene de Excel y
# los encabezados llegan con mayusculas y separaciones distintas segun quien lo
# exporto.
COLUMNAS = ("label", "x", "y", "z", "detonatingtime")


def _indices(encabezado: list[str]) -> Optional[list[int]]:
    """Ubica las 5 columnas por nombre. Devuelve None si el encabezado no sirve."""
    norm = [c.strip().lower().replace(" ", "").replace("_", "") for c in encabezado]
    try:
        return [norm.index(c) for c in COLUMNAS]
    except ValueError:
        return None


def _es_dato(fila: list[str], idx: list[int]) -> bool:
    """True si la fila tiene numeros donde corresponde (o sea: no es encabezado)."""
    if len(fila) <= max(idx):
        return False
    try:
        [float(fila[i]) for i in idx[1:]]
        return True
    except (ValueError, TypeError):
        return False


def leer_csv(contenido: bytes) -> list[dict]:
    """Devuelve los pozos del CSV: {id, x, y, z, t}.

    Tolerante a proposito: el archivo lo arma una persona en Excel y suele traer
    filas de resumen al final o encabezados repetidos al concatenar tronaduras.
    La condicion de fila valida es que el tiempo de detonacion sea un numero
    (misma regla que usa la calibracion en debug/malla_export.py). Una fila que
    no cumple se salta en silencio en vez de tumbar el analisis completo.
    """
    texto = contenido.decode("utf-8-sig", errors="replace")
    filas = list(csv.reader(io.StringIO(texto)))
    if not filas:
        raise ValueError("El CSV de secuencia esta vacio.")

    idx = _indices(filas[0])
    if idx is None:
        # Sin encabezado reconocible caemos al orden posicional documentado.
        # No es adivinanza: es el formato que produce el software de la mina.
        idx = [0, 1, 2, 3, 4]
        # Y entonces la primera fila puede ser un pozo, no un titulo: si sus
        # numeros parsean, entra. Descartarla de oficio perdia el pozo 101.
        cuerpo = filas if _es_dato(filas[0], idx) else filas[1:]
    else:
        cuerpo = filas[1:]

    pozos = []
    for fila in cuerpo:
        if len(fila) <= max(idx):
            continue
        try:
            t = float(fila[idx[4]])
            x = float(fila[idx[1]])
            y = float(fila[idx[2]])
            z = float(fila[idx[3]])
        except (ValueError, TypeError):
            continue
        etiqueta = fila[idx[0]].strip()
        if not etiqueta:
            continue
        pozos.append({"id": etiqueta, "x": x, "y": y, "z": z, "t": t})

    if not pozos:
        raise ValueError(
            "El CSV no tiene ninguna fila valida. Se esperan las columnas "
            "Label, X, Y, Z, DetonatingTime."
        )
    return pozos


def proyectar(pozos: list[dict], h_matrix, fps: Optional[float] = None) -> dict:
    """Proyecta los pozos a pixeles con la homografia del analisis.

    `h_matrix` es la misma que recibe el endpoint: llega como [[9 numeros]] y se
    reordena a 3x3, igual que en trajectory_categorization.py. Va de mundo a
    pixel (el nodo de categorizacion usa su inversa para el camino contrario).

    Las coordenadas de mundo se devuelven CENTRADAS en el centroide de la malla.
    Los valores originales rondan los 100.000 m y en float32 de JavaScript
    pierden precision util; el centroide viaja aparte, en `meta`.
    """
    H = np.array(h_matrix, dtype=np.float64).reshape(3, 3)

    W = np.array([[p["x"], p["y"]] for p in pozos], dtype=np.float64)
    T = np.array([p["t"] for p in pozos], dtype=np.float64)
    centro = W.mean(0)

    hom = np.column_stack([W, np.ones(len(W))])
    q = (H @ hom.T).T
    denom = q[:, 2:3]
    if np.any(np.abs(denom) < 1e-12):
        raise ValueError("La homografia manda algun pozo al infinito.")
    px = q[:, :2] / denom

    # Reabsorbemos el centroide en la traslacion para que el consumidor pueda
    # reproyectar si mueve la malla: px = A @ (mundo - centro) + t2. Solo vale si
    # la homografia es afin en la practica (fila 3 ~ [0,0,1]), que es el caso de
    # una vista cenital; si no lo fuera, `px` sigue siendo correcto y esto es un
    # atajo que el consumidor puede ignorar.
    A = H[:2, :2]
    t2 = H[:2, :2] @ centro + H[:2, 2]
    err = float(np.abs(np.column_stack([W - centro, np.ones(len(W))]) @
                       np.vstack([A.T, t2]) - px).max())

    return {
        "meta": {
            "fuente": "csv_secuencia",
            "n_pozos": len(pozos),
            "centro_mundo": [float(centro[0]), float(centro[1])],
            "t_min": float(T.min()),
            "t_max": float(T.max()),
            "fps": fps,
            # Frame donde detona el primer tiro. NO se deduce del CSV: el CSV da
            # tiempos relativos entre pozos, no el instante del video en que
            # arranca la secuencia. Hoy lo fija el usuario en la vista; el
            # camino automatico es detectar el destello (pendiente P8).
            "frame_inicio": None,
            "A": [[float(v) for v in fila] for fila in A],
            "t": [float(v) for v in t2],
            "error_reparametrizacion_px": round(err, 6),
        },
        # x,y = mundo centrado (m) | px,py = pixel | z = cota (m) | t = ms
        "pozos": [
            {
                "id": p["id"],
                "x": round(float(W[i, 0] - centro[0]), 3),
                "y": round(float(W[i, 1] - centro[1]), 3),
                "z": p["z"],
                "px": round(float(px[i, 0]), 1),
                "py": round(float(px[i, 1]), 1),
                "t": p["t"],
            }
            for i, p in enumerate(pozos)
        ],
    }


def desde_csv(contenido: bytes, h_matrix, fps: Optional[float] = None) -> dict:
    """Atajo: CSV crudo -> malla proyectada."""
    return proyectar(leer_csv(contenido), h_matrix, fps)
