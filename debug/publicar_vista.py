"""Copia la vista al frontend, para que entre en la imagen de nginx.

La vista se ITERA en debug/demo/vista.html: ahi la sirve caso_serve.py contra
casos congelados, sin levantar el wizard ni el core. Pero para que el cliente la
vea tiene que estar dentro de la imagen del frontend, que se construye desde
otro repositorio.

Son dos repos distintos, asi que un symlink no sirve (y en Windows es peor). La
alternativa honesta es este paso explicito: una sola fuente de verdad
(debug/demo/) y una copia marcada como generada, que se rehace antes de
construir. Si alguien edita la copia, el aviso de arriba se lo dice.

    uv run python debug/publicar_vista.py

Correr esto ANTES de `docker compose build konva-app`.
"""
import hashlib
import sys
from pathlib import Path

ORIGEN = Path(__file__).resolve().parent / "demo" / "vista.html"
DESTINO = (Path(__file__).resolve().parents[3] / "detovision_standalone" /
           "flyRocks_frontend" / "public" / "vista.html")

AVISO = """<!-- ARCHIVO GENERADO - NO EDITAR ACA.
     La fuente es flyrocks_core/debug/demo/vista.html.
     Se regenera con: uv run python debug/publicar_vista.py
     Editar esta copia se pierde en la siguiente publicacion. -->
"""


def main():
    if not ORIGEN.exists():
        raise SystemExit(f"no existe {ORIGEN}")
    if not DESTINO.parent.exists():
        raise SystemExit(f"no existe {DESTINO.parent} - revisar la ruta al frontend")

    fuente = ORIGEN.read_text(encoding="utf-8")
    nuevo = AVISO + fuente

    if DESTINO.exists():
        actual = DESTINO.read_text(encoding="utf-8")
        if actual == nuevo:
            print(f"sin cambios: {DESTINO}")
            return
        # Si la copia fue editada a mano (no coincide con ninguna version del
        # origen), avisamos antes de pisarla. Es barato y evita perder trabajo.
        if not actual.startswith("<!-- ARCHIVO GENERADO"):
            print(f"OJO: {DESTINO} no parece generado por este script.",
                  file=sys.stderr)
            print("Se sobrescribe igual, pero revisa si tenia cambios propios.",
                  file=sys.stderr)

    DESTINO.write_text(nuevo, encoding="utf-8")
    h = hashlib.sha256(fuente.encode("utf-8")).hexdigest()[:12]
    print(f"publicada: {DESTINO}")
    print(f"  {len(fuente)/1024:.0f} KB  sha256:{h}")
    print("  queda servida por nginx en /vista.html")
    empujar_al_contenedor()


def empujar_al_contenedor():
    """Copia la vista al nginx que ya esta corriendo, sin reconstruir la imagen.

    Iterar la vista no deberia costar un `npm run build` de un minuto por cada
    ajuste. Como es un estatico, basta dejarlo dentro del contenedor vivo: al
    recargar la pagina ya esta el cambio. La imagen queda desactualizada hasta el
    siguiente build, y eso esta bien: la imagen es la entrega, esto es el taller.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=konva-app", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    nombres = [n for n in r.stdout.split() if n]
    if not nombres:
        print("  (no hay contenedor konva-app corriendo: se aplica al reconstruir)")
        return

    for nombre in nombres:
        c = subprocess.run(
            ["docker", "cp", str(DESTINO), f"{nombre}:/usr/share/nginx/html/vista.html"],
            capture_output=True, text=True, timeout=60)
        if c.returncode == 0:
            print(f"  copiada en caliente a {nombre} - recarga la pagina y listo")
        else:
            print(f"  no se pudo copiar a {nombre}: {c.stderr.strip()}")


if __name__ == "__main__":
    main()
