import hashlib
import json
import logging
import os
import pickle
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Cache por nodo
# ---------------------------------------------------------------------------
#
# El pipeline son 13 nodos en cadena y hoy se corren TODOS siempre. Tocar el
# tracker (nodo 5) obliga a pagar de nuevo la extraccion del video (nodo 1, ~20 s)
# y el GridSearch (nodo 4, ~60 s de los 92 totales) para volver a calcular
# exactamente lo mismo. Iterar sobre deteccion y tracking se vuelve carisimo.
#
# La idea es darle a cada nodo una LLAVE ENCADENADA:
#
#     llave(nodo) = hash( clase + parametros + llave(nodo anterior) )
#
# La salida de cada nodo se guarda bajo su llave. En la corrida siguiente:
#
#   - cambiaste solo el tracker  -> los nodos 1-4 conservan su llave, se leen
#                                   de disco; se recalcula del 5 en adelante.
#   - cambiaste `percentile`     -> cambia la llave del nodo 2 y, por
#     (nodo 2)                      encadenamiento, la de todos los siguientes.
#                                   Se recalcula del 2 en adelante.
#   - no cambiaste nada          -> todo cacheado.
#
# No hay flags de invalidacion ni riesgo de leer una cache vieja: si la entrada
# cambio, la llave cambio. La correccion sale de la identidad, no de que
# alguien se acuerde de limpiar.
#
# Se apaga con PIPELINE_CACHE=0.

CACHE_ACTIVA = os.getenv("PIPELINE_CACHE", "1") not in ("0", "false", "False")
CACHE_DIR = Path(os.getenv("DATA_DIR", "data")) / "cache"

# Techo de la cache en MB. Cada configuracion distinta suma objetos y nadie los
# borra nunca: medido, tres configuraciones dejaban 74 MB y eso crece sin
# limite. En desarrollo da igual, en la maquina de un cliente no.
#
# 2 GB por defecto son ~30 configuraciones del caso de referencia (60 MB cada
# una), de sobra para trabajar sin que la carpeta se coma el disco. Con 0 (o
# negativo) no se purga nunca.
CACHE_MAX_MB = float(os.getenv("CACHE_MAX_MB", "2048"))


def _memoria_mb() -> float:
    """Memoria residente del proceso, en MB. Sin dependencias nuevas: en Linux
    sale de /proc, que es donde corre el contenedor."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:
        return 0.0


def _hash_json(obj: Any) -> str:
    """Hash estable de cualquier cosa serializable a JSON."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


def llave_de_entrada(context: Dict[str, Any]) -> str:
    """Llave del contexto inicial: identifica el trabajo antes del primer nodo.

    El video entra por su ruta MAS su tamaño y fecha: dos archivos distintos
    con el mismo nombre no pueden compartir cache, que es justo lo que pasaria
    al reprocesar 'video.mp4' de otra tronadura.
    """
    partes: Dict[str, Any] = {}
    for k, v in context.items():
        if k == "video_path" and v and os.path.exists(str(v)):
            st = os.stat(str(v))
            partes[k] = [os.path.basename(str(v)), st.st_size, int(st.st_mtime)]
        else:
            partes[k] = v
    return _hash_json(partes)[:16]


class PipelineNode(ABC):
    """
    Abstract base class for a processing node.
    """

    # Un nodo puede excluirse de la cache si su salida es enorme y barata de
    # recalcular. Por defecto todos cachean.
    cacheable: bool = True

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        pass

    def __or__(self, other: 'PipelineNode') -> 'PipelineChain':
        return PipelineChain(self, other)

    def get_nodes(self) -> List['PipelineNode']:
        """Returns itself as a list to help flattening the chain."""
        return [self]

    # -- cache ---------------------------------------------------------------

    def parametros(self) -> Dict[str, Any]:
        """Lo que define la identidad de este nodo.

        Por defecto, todos sus atributos menos el nombre (que es una etiqueta y
        no cambia el resultado). Un nodo con parametros no serializables puede
        redefinir esto.
        """
        return {k: v for k, v in vars(self).items() if k != "name"}

    def llave(self, llave_previa: str) -> str:
        return _hash_json({
            "clase": self.__class__.__name__,
            "params": self.parametros(),
            "previa": llave_previa,
        })[:16]


class PipelineChain(PipelineNode):
    """
    A composite node that executes multiple nodes sequentially.
    """
    def __init__(self, first: PipelineNode, second: PipelineNode):
        super().__init__(f"Chain")
        self.first = first
        self.second = second

    def get_nodes(self) -> List[PipelineNode]:
        """Flattens the entire chain into a single linear list of nodes."""
        return self.first.get_nodes() + self.second.get_nodes()

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return ejecutar(self.get_nodes(), context)


# ---------------------------------------------------------------------------
#  Ejecucion con cache
# ---------------------------------------------------------------------------

OBJ_DIR = CACHE_DIR / "objetos"


def _escribir_bytes(ruta: Path, blob: bytes) -> None:
    """A un temporal y luego renombrar: si el proceso muere a mitad, no queda
    un archivo truncado que despues se lea como valido."""
    tmp = ruta.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
    tmp.replace(ruta)


def _leer(ruta: Path) -> Dict[str, Any]:
    with open(ruta, "rb") as f:
        indice = pickle.load(f)
    ctx = {}
    for clave, h in indice.items():
        with open(OBJ_DIR / f"{h}.bin", "rb") as f:
            ctx[clave] = pickle.load(f)
    return ctx


def _escribir(ruta: Path, ctx: Dict[str, Any]) -> bool:
    """Guarda el contexto DEDUPLICADO por contenido.

    Cada valor se escribe una sola vez bajo el hash de sus bytes, y la entrada
    del nodo es apenas un indice {clave: hash}. Sin esto, cada uno de los 13
    nodos guardaba el contexto completo y el mismo tensor de ~45 MB terminaba
    escrito doce veces: 659 MB de cache medidos para UNA corrida. Como la
    mayoria de las claves no cambia de un nodo al siguiente, casi todas las
    escrituras despues de la primera se saltan.
    """
    try:
        OBJ_DIR.mkdir(parents=True, exist_ok=True)
        indice: Dict[str, str] = {}
        for clave, valor in ctx.items():
            blob = pickle.dumps(valor, protocol=pickle.HIGHEST_PROTOCOL)
            h = hashlib.sha256(blob).hexdigest()[:16]
            destino = OBJ_DIR / f"{h}.bin"
            if not destino.exists():
                _escribir_bytes(destino, blob)
            indice[clave] = h
        _escribir_bytes(ruta, pickle.dumps(indice, protocol=pickle.HIGHEST_PROTOCOL))
        return True
    except Exception as e:
        logger.warning(f"[cache] no se pudo guardar ({e}); se sigue sin cache")
        return False


def purgar(limite_mb: float = None) -> int:
    """Baja la cache del techo configurado tirando las entradas mas viejas.

    Devuelve los bytes liberados.

    Por que no basta con borrar los `.pkl` viejos: los objetos estan
    DEDUPLICADOS. Una entrada es un indice {clave: hash} y varios indices
    apuntan al mismo `objetos/<hash>.bin` — de hecho ese es el punto del diseño,
    y es lo que baja una corrida de 659 MB a 60 MB. Borrar los objetos de la
    entrada que se va se llevaria por delante los de las que se quedan. Asi que
    esto es mark & sweep: se descartan entradas por antiguedad y despues se
    borra solo lo que ya no referencia NADIE.

    Se purga por antiguedad de la entrada (mtime). La corrida que acaba de
    escribir es la mas nueva, asi que nunca se purga a si misma.

    Concurrencia: si otra corrida esta leyendo un objeto en el momento en que se
    borra, `ejecutar` ya trata el fallo de lectura como cache miss y recalcula
    (ver el try/except del HIT). El peor caso es perder tiempo, no romper.
    """
    limite = (CACHE_MAX_MB if limite_mb is None else limite_mb) * 1024 * 1024
    if limite <= 0 or not CACHE_DIR.exists():
        return 0

    entradas = sorted(CACHE_DIR.glob("*.pkl"), key=lambda p: p.stat().st_mtime)
    objetos = {p.name: p.stat().st_size for p in OBJ_DIR.glob("*.bin")} if OBJ_DIR.exists() else {}
    total = sum(objetos.values()) + sum(p.stat().st_size for p in entradas)
    if total <= limite:
        return 0

    # Que objetos referencia cada entrada. Un indice ilegible se trata como
    # entrada muerta: no referencia nada y se va en la primera pasada.
    refs: Dict[Path, set] = {}
    for e in entradas:
        try:
            with open(e, "rb") as f:
                refs[e] = set(pickle.load(f).values())
        except Exception:
            refs[e] = set()

    # Se descartan de la mas vieja a la mas nueva hasta entrar en el techo,
    # midiendo en cada paso lo que quedaria vivo de verdad.
    #
    # La ULTIMA nunca se condena. Es la que el pipeline acaba de escribir, y si
    # el techo quedo por debajo de lo que pesa una corrida, purgarla dejaria la
    # cache escribiendo y borrando lo mismo en cada vuelta: nunca un HIT, y el
    # costo de escribirla pagado siempre. Mejor pasarse del techo y decirlo.
    condenadas: List[Path] = []
    for i, e in enumerate(entradas[:-1]):
        condenadas.append(e)
        vivos = set().union(*(refs[x] for x in entradas[i + 1:]))
        quedaria = (
            sum(t for h, t in objetos.items() if h.removesuffix(".bin") in vivos)
            + sum(p.stat().st_size for p in entradas[i + 1:])
        )
        if quedaria <= limite:
            break
    else:
        if entradas:
            logger.warning(
                f"[cache] el techo ({limite / 1024 / 1024:.0f} MB) es menor que "
                f"una sola corrida: se conserva la ultima entrada igual. "
                f"Sube CACHE_MAX_MB o apaga la cache con PIPELINE_CACHE=0.")

    vivos = set().union(*(refs[x] for x in entradas if x not in condenadas)) if len(condenadas) < len(entradas) else set()
    liberado = 0
    for e in condenadas:
        liberado += e.stat().st_size
        e.unlink(missing_ok=True)
    for nombre, tam in objetos.items():
        if nombre.removesuffix(".bin") not in vivos:
            (OBJ_DIR / nombre).unlink(missing_ok=True)
            liberado += tam

    logger.info(
        f"[cache] purga: {len(condenadas)} entradas y "
        f"{liberado / 1024 / 1024:.0f} MB liberados "
        f"(techo {limite / 1024 / 1024:.0f} MB)")
    return liberado


def ejecutar(nodes: List[PipelineNode], context: Dict[str, Any],
             progreso=None) -> Dict[str, Any]:
    """Corre la cadena reusando lo que ya este calculado.

    `progreso(i, total, nodo)` se llama antes de cada nodo que SI se ejecuta,
    para que el servicio reporte avance sin tener que saber de la cache.
    """
    total = len(nodes)
    llave = llave_de_entrada(context)

    if CACHE_ACTIVA:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Se busca el punto mas avanzado ya calculado y se arranca desde ahi, en
    # vez de ir preguntando nodo a nodo: si el nodo 9 esta en cache, no hace
    # falta ni mirar los ocho anteriores.
    llaves = []
    for node in nodes:
        llave = node.llave(llave)
        llaves.append(llave)

    inicio = 0
    if CACHE_ACTIVA:
        for i in range(total - 1, -1, -1):
            if not nodes[i].cacheable:
                continue
            ruta = CACHE_DIR / f"{llaves[i]}.pkl"
            if ruta.exists():
                try:
                    t0 = time.time()
                    context = _leer(ruta)
                    logger.info(
                        f"[cache] HIT hasta '{nodes[i].name}' "
                        f"({i + 1}/{total}) en {time.time() - t0:.1f}s "
                        f"— se saltan {i + 1} nodos")
                    inicio = i + 1
                    break
                except Exception as e:
                    logger.warning(f"[cache] archivo ilegible ({e}), se recalcula")
                    ruta.unlink(missing_ok=True)

    for i in range(inicio, total):
        node = nodes[i]
        logger.info(f"\n---> [Step {i + 1}/{total}] Running: {node.name} <---")
        if progreso:
            progreso(i, total, node)

        context = node.run(context)

        if "error" in context:
            logger.error(f"Pipeline halted at '{node.name}': {context['error']}")
            return context

        if CACHE_ACTIVA and node.cacheable:
            _escribir(CACHE_DIR / f"{llaves[i]}.pkl", context)

        # Cuanta memoria dejo cada nodo. Sin esto, un OOM en la maquina del
        # cliente es un contenedor que muere sin decir por que; con esto se ve
        # que nodo lo hizo crecer y con cuanto margen quedo.
        logger.info(f"[pipeline] {node.name}: {_memoria_mb():.0f} MB en uso")

    # Al final y no al arrancar: asi lo que se acaba de calcular ya cuenta, y
    # una purga lenta no retrasa el primer nodo. Que falle no puede costar el
    # resultado del pipeline, que es lo caro.
    if CACHE_ACTIVA:
        try:
            purgar()
        except Exception as e:
            logger.warning(f"[cache] no se pudo purgar ({e}); se sigue igual")

    return context
