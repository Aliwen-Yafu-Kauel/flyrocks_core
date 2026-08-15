"""Arma el paquete que se le entrega al cliente, desde el workspace.

    uv run python entrega/armar_paquete.py [destino]

Sin destino, escribe en `entrega/salida/Detovision/`.

Produce exactamente la forma que el cliente ya conoce (la del Detovision_V3):

    Detovision/
        detovision.bat           el menu de 3 opciones que abre el usuario
        Dependencias/            instalador de Docker Desktop   (*)
        Intrucciones/            los dos videos y el README     (*)
        mvp/
            docker-compose.yml
            flyRocks_frontend/
            flyrocks_core/
            flyrocks_blast_detector/

    (*) Estas dos NO se generan: pesan cientos de MB y no cambian. Se copian del
        paquete anterior con --extras <ruta>, y si no se pasa, el script avisa y
        deja el paquete sin ellas.

Por que existe esto en vez de copiar a mano: el paquete anterior se armo
copiando carpetas, y en el camino se quedo sin volumenes (el cliente perdia
todo en cada sesion) y con el blast detector bajo su nombre de la v3. Un
paquete que se arma con un comando se puede rehacer igual la proxima vez.

QUE NO ENTRA, y por que importa:

  - `.git`, `.venv`, `node_modules`, `dist`, `__pycache__` — se reconstruyen
    solos y multiplican el tamaño por diez.
  - `flyrocks_core/debug/` ENTERO — son nuestros casos congelados, videos de
    cliente, experimentos y notas internas. Ahi hay varios GB y cosas que no
    corresponde mandar. El core no lo necesita para correr: su Dockerfile solo
    copia `pyproject.toml`, `uv.lock` y `src/`.
  - `data/`, `temp_videos/`, `*.db` — estado local de desarrollo. Si viajaran,
    el cliente arrancaria con nuestros analisis dentro.

La vista SI entra, porque vive en `flyRocks_frontend/public/vista.html` (una
copia generada). El script republica antes de copiar, para no empaquetar una
version vieja.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CORE = AQUI.parent
WORKSPACE = CORE.parent                     # detovision_standalone/

# nombre en el paquete  ->  carpeta en el workspace
SERVICIOS = {
    "flyRocks_frontend": "flyRocks_frontend",
    "flyrocks_core": "flyrocks_core",
    "flyrocks_blast_detector": "flyrocks_blast_detector",
}

COMUNES = {
    ".git", ".github", ".venv", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vscode",
    ".idea", ".DS_Store",
    # Configuracion de nuestras herramientas de trabajo. No es codigo del
    # producto y no tiene por que salir de acá: `settings.local.json` guarda
    # permisos y rutas de esta maquina. Se colo en la primera version del
    # paquete y lo detecto la comparacion contra el que se entrego.
    ".claude", ".cursor", ".aider.conf.yml",
}

# El resto de los ocultos SI viajan, porque tambien viajaban en los paquetes ya
# entregados y algunos hacen falta: `.env` (Vite resuelve las VITE_URL_* en el
# build), `.dockerignore` (lo usa el build del core), `.python-version`,
# `.gitignore` y `.gitattributes`. Verificado contra Detovision_V3: con esta
# exclusion, la lista de ocultos del paquete queda identica a la de aquel.

# Lo que sobra de cada servicio, ademas de COMUNES.
PROPIAS = {
    "flyrocks_core": {"debug", "data", "temp_videos", "entrega", "out"},
    "flyrocks_blast_detector": {"data", "uploads", "thumbnails"},
    "flyRocks_frontend": set(),
}

SUFIJOS_FUERA = {".db", ".pyc", ".log", ".mp4", ".zip"}


def ignorar_de(servicio: str):
    fuera = COMUNES | PROPIAS.get(servicio, set())

    def _ignorar(directorio, nombres):
        saltar = set()
        for n in nombres:
            if n in fuera or Path(n).suffix.lower() in SUFIJOS_FUERA:
                saltar.add(n)
        return saltar

    return _ignorar


def pesar(ruta: Path) -> int:
    return sum(f.stat().st_size for f in ruta.rglob("*") if f.is_file())


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Arma el paquete de entrega.")
    ap.add_argument("destino", nargs="?", default=str(AQUI / "salida" / "Detovision"))
    ap.add_argument("--extras", help="Paquete anterior del que copiar "
                                     "Dependencias/ e Intrucciones/")
    ap.add_argument("--sin-publicar", action="store_true",
                    help="No republicar la vista antes de copiar")
    ap.add_argument("--vista", choices=("nueva", "anterior"), default="nueva",
                    help="Con cual de las dos vistas arranca el paso 4")
    ap.add_argument("--zip", metavar="NOMBRE",
                    help="Comprime el paquete como NOMBRE.zip junto al destino")
    args = ap.parse_args()

    destino = Path(args.destino).resolve()
    mvp = destino / "mvp"

    # 1. La vista al dia. Se itera en debug/demo/ y viaja en public/ del front:
    #    si no se republica, el paquete sale con la vista de la vuelta anterior
    #    y nadie se entera hasta que el cliente la abre.
    if not args.sin_publicar:
        pub = CORE / "debug" / "publicar_vista.py"
        if pub.exists():
            print("[1/4] republicando la vista...")
            r = subprocess.run([sys.executable, str(pub)], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ERROR al publicar la vista:\n{r.stdout}{r.stderr}")
                return 1
            print("  " + r.stdout.strip().splitlines()[0].strip())
        else:
            print("[1/4] no encuentro publicar_vista.py; sigo sin republicar")
    else:
        print("[1/4] republicacion saltada (--sin-publicar)")

    # 2. Limpiar el destino. Solo se borra lo que este script genera.
    if destino.exists():
        print(f"[2/4] limpiando {destino}")
        try:
            shutil.rmtree(destino)
        except PermissionError as e:
            # En Windows basta con tener una consola abierta dentro de la
            # carpeta para que no se pueda borrar. El traceback de shutil no
            # dice eso y manda a buscar el problema donde no esta.
            print(f"  No se pudo borrar el paquete anterior: {e.filename}")
            print("  Suele ser una terminal o un explorador abierto ahi dentro.")
            print("  Cierra eso (o pasa otro destino) y volve a intentar.")
            return 1
    mvp.mkdir(parents=True)

    # 3. Los tres servicios.
    print("[3/4] copiando servicios")
    total = 0
    for nombre, carpeta in SERVICIOS.items():
        origen = WORKSPACE / carpeta
        if not origen.is_dir():
            print(f"  FALTA {origen} — el paquete quedaria incompleto")
            return 1
        shutil.copytree(origen, mvp / nombre, ignore=ignorar_de(carpeta))
        peso = pesar(mvp / nombre)
        total += peso
        print(f"  {nombre:<26} {mb(peso):>10}")

    shutil.copy2(AQUI / "docker-compose.yml", mvp / "docker-compose.yml")
    shutil.copy2(AQUI / "detovision.bat", destino / "detovision.bat")

    # La vista tiene que haber viajado: es el ultimo paso del wizard.
    vista = mvp / "flyRocks_frontend" / "public" / "vista.html"
    if not vista.exists():
        print("  AVISO: no viajo public/vista.html — el paso 4 nuevo no va a cargar")

    # Con cual de las dos vistas arranca el paso 4, ESCRITO. Sin la variable el
    # codigo tambien arranca con la nueva (`undefined !== "false"`), pero eso es
    # funcionar por ausencia: si alguien agrega la variable con cualquier otro
    # valor, la entrega cambia de vista sin que nadie lo haya decidido. Vite la
    # resuelve en tiempo de build, asi que queda fijada en el bundle.
    env = mvp / "flyRocks_frontend" / ".env"
    valor = "true" if args.vista == "nueva" else "false"
    if env.exists():
        texto = env.read_text(encoding="utf-8").rstrip("\n")
        lineas = [l for l in texto.splitlines() if not l.startswith("VITE_VISTA_NUEVA")]
        lineas.append(f"VITE_VISTA_NUEVA={valor}")
        env.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        cual = "la nueva (vista.html)" if args.vista == "nueva" else "la anterior (Step4)"
        print(f"  paso 4 arranca con {cual}  [VITE_VISTA_NUEVA={valor}]")
    else:
        print("  AVISO: el frontend no trae .env — se construiria sin backend")

    # 4. Los extras, que no se generan.
    print("[4/4] extras")
    if args.extras:
        base = Path(args.extras)
        for carpeta in ("Dependencias", "Intrucciones"):
            o = base / carpeta
            if o.is_dir():
                shutil.copytree(o, destino / carpeta)
                print(f"  {carpeta:<26} {mb(pesar(destino / carpeta)):>10}")
            else:
                print(f"  {carpeta}: no esta en {base}")
    else:
        print("  sin --extras: el paquete queda SIN Dependencias/ ni Intrucciones/.")
        print("  Copialas del paquete anterior antes de entregar, o pasa")
        print("  --extras <ruta del paquete viejo>.")

    print(f"\n  paquete en {destino}")
    print(f"  {mb(pesar(destino))} en total")

    # El zip lleva la carpeta `Detovision/` adentro, no su contenido suelto:
    # asi se descomprime igual que los paquetes anteriores y el usuario
    # encuentra el .bat donde espera.
    if args.zip:
        nombre = args.zip[:-4] if args.zip.lower().endswith(".zip") else args.zip
        archivo = destino.parent / nombre
        if archivo.with_suffix(".zip").exists():
            archivo.with_suffix(".zip").unlink()
        print(f"\n  comprimiendo...")
        shutil.make_archive(str(archivo), "zip",
                            root_dir=str(destino.parent),
                            base_dir=destino.name)
        z = archivo.with_suffix(".zip")
        print(f"  {z}")
        print(f"  {mb(z.stat().st_size)}")

    print(f"\n  Para probarlo: cd \"{mvp}\" && docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
