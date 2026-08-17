"""
Sirve `debug/` por HTTP para que la vista pueda cargar un caso.

Hace falta porque abrir el HTML con doble clic (file://) hace que el navegador
bloquee por CORS cualquier fetch a los archivos del caso. La demo vieja se
libraba de esto porque tenia la malla embebida; con la mascara y el clip
adentro el HTML se iria a decenas de MB.

    uv run python debug/caso_serve.py
    -> http://localhost:8770/demo/vista.html
"""
import http.server
import os
import re
import socketserver
import sys
import threading
import webbrowser
from functools import partial
from pathlib import Path

DIR = Path(__file__).resolve().parent
PUERTO = 8770
INICIO = "demo/vista.html"

RANGO = re.compile(r"bytes=(\d*)-(\d*)$")


class Recorte:
    """Un archivo que se termina a los N bytes.

    `copyfile` vacia el objeto hasta EOF, asi que para responder un trozo hay
    que darle uno que se acabe donde toca.
    """

    def __init__(self, f, n):
        self.f, self.n = f, n

    def read(self, tam=-1):
        if self.n <= 0:
            return b""
        if tam is None or tam < 0:
            tam = self.n
        d = self.f.read(min(tam, self.n))
        self.n -= len(d)
        return d

    def close(self):
        self.f.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    # Con HTTP/1.0 cada trozo del video abre y cierra su conexion, y el clip se
    # pide de a pedazos: son cientos de conexiones para reproducir 15 segundos.
    # Con 1.1 se reusa la misma. Es seguro porque todas las respuestas de aca
    # llevan Content-Length.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        # Sin cache: si no, editas el caso o el HTML y el navegador te sigue
        # mostrando el anterior, que es media hora de confusion garantizada.
        # El video es la excepcion: son decenas de MB que no cambian entre
        # ediciones de la vista, y volver a bajarlos en cada recarga hace lento
        # justo lo que uno esta iterando.
        if self.path.endswith(".mp4"):
            self.send_header("Cache-Control", "max-age=3600")
        else:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        """Igual que el de la casa, salvo que respeta `Range`.

        Sin esto el navegador tiene que bajar el clip entero antes de mostrar
        nada, y avanzar frame a frame —que es puro salto— se vuelve inusable:
        `SimpleHTTPRequestHandler` ignora la cabecera y responde 200 con todo
        el archivo, y Chrome directamente no deja hacer seek.
        """
        cabecera = self.headers.get("Range")
        m = RANGO.match(cabecera.strip()) if cabecera else None
        if not m:
            return super().send_head()

        ruta = self.translate_path(self.path)
        if os.path.isdir(ruta):
            return super().send_head()
        try:
            f = open(ruta, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        total = os.fstat(f.fileno()).st_size
        ini, fin = m.group(1), m.group(2)
        if ini == "":
            # "bytes=-N": los ultimos N bytes. Lo usan los reproductores para
            # leer el indice del MP4 cuando quedo al final del archivo.
            ini, fin = max(0, total - int(fin or 0)), total - 1
        else:
            ini = int(ini)
            fin = int(fin) if fin else total - 1
        fin = min(fin, total - 1)

        if ini >= total or ini > fin:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            # Sin Content-Length el cliente se queda esperando un cuerpo que no
            # existe, y con keep-alive ademas descoloca la conexion entera.
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        f.seek(ini)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(ruta))
        self.send_header("Content-Range", f"bytes {ini}-{fin}/{total}")
        self.send_header("Content-Length", str(fin - ini + 1))
        self.end_headers()
        return Recorte(f, fin - ini + 1)

    def log_message(self, fmt, *args):
        # Los 206 son el goteo del video: uno por salto, y ensucian el log
        # hasta tapar lo que uno estaba mirando.
        linea = fmt % args
        if "304" not in linea and " 206 " not in linea:
            print(f"  {linea}", flush=True)


class Servidor(socketserver.ThreadingTCPServer):
    """Un hilo por peticion.

    Con el TCPServer normal (una peticion a la vez) el navegador se queda con
    el servidor mientras baja el frame de 12 MB, y todo lo demas espera en cola
    hasta agotar su timeout: la vista parece colgada. Con imagenes de este
    tamaño el servidor concurrente no es un lujo.
    """
    # OJO: `allow_reuse_address` queda en False a proposito. En Windows esa
    # opcion permite que DOS procesos escuchen el mismo puerto a la vez, y las
    # conexiones caen en uno u otro de forma impredecible: media hora perdida
    # persiguiendo por que el navegador cargaba y curl daba timeout. Mejor que
    # el segundo arranque falle con un mensaje claro.
    daemon_threads = True

    # La cola por defecto (5) se llena apenas el video entra en escena: el
    # navegador abre varias conexiones para pedir trozos del clip mientras
    # todavia bajan la mascara y el frame, el SO empieza a rechazar y el
    # <video> se queda cargando para siempre sin dar un error.
    request_queue_size = 128


def ya_hay_uno():
    """True si algo ya escucha en el puerto."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PUERTO)) == 0


def main():
    abrir = "--no-abrir" not in sys.argv

    if ya_hay_uno():
        print(f"\n  Ya hay un servidor en el puerto {PUERTO}.")
        print(f"  Abre http://localhost:{PUERTO}/{INICIO}")
        print(f"  (si quieres reiniciarlo, cierra el otro con ctrl-c primero)\n")
        return
    with Servidor(("", PUERTO), partial(Handler, directory=str(DIR))) as s:
        url = f"http://localhost:{PUERTO}/{INICIO}"
        print(f"\n  sirviendo {DIR}\n  -> {url}\n  (ctrl-c para parar)\n")
        if abrir:
            # En un hilo aparte: si el navegador tarda en levantarse, el
            # servidor ya esta aceptando conexiones igual.
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            print("\n  chao\n")


if __name__ == "__main__":
    main()
