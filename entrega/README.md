# Entrega — el paquete que corre el cliente

Lo que el cliente recibe no es un repositorio: es una carpeta con un `.bat`, el
instalador de Docker y los tres servicios en código fuente, que se construyen en
su máquina. Acá vive lo que define ese paquete, para no volver a armarlo a mano.

    uv run python entrega/armar_paquete.py

El **v8** (2026-08-14) se armó en dos variantes, idénticas salvo por el
instalador de Docker:

    # sin Dependencias — mismo contenido que el v7, ~12 MB
    uv run python entrega/armar_paquete.py \
        --extras "C:/Users/carlo/Downloads/Detovision_v7/Detovision" \
        --vista nueva --zip Detovision_v8

    # con Dependencias — para quien no tenga Docker instalado, ~600 MB
    uv run python entrega/armar_paquete.py \
        --extras "C:/Users/carlo/Downloads/Detovision_V3/Detovision" \
        --vista nueva --zip Detovision_v8_con_dependencias

La diferencia entre los dos `--extras` es que el v7 ya no traía
`Dependencias/` (591 MB, el instalador de Docker Desktop) y el v3 sí. Las
`Intrucciones/` de ambos son idénticas, verificado, así que el `mvp/` y el
`detovision.bat` salen exactamente iguales en las dos variantes.

**El frontend compila.** `npm run build` (que es `tsc -b && vite build`, lo
mismo que corre el Dockerfile) pasa en verde con los cambios de P0 y del
wizard: 1078 módulos, sin errores de tipos. Era el riesgo real de esta entrega,
porque un error de TypeScript no falla acá sino en el `--build` de la máquina
del cliente.

## El menú del `.bat` (cambiado en el v8)

Hasta el v7 el menú era «INICIAR / DETENER / SALIR», y «detener» no decía qué
pasaba con el trabajo del usuario. Ahora que los datos **persisten** (los
volúmenes del compose), eso se volvió una pregunta real: quien quiera limpiar su
equipo no tiene cómo, y quedan datos en volúmenes de Docker que nadie sabe que
existen ni cómo borrar.

| Opción | Qué dice | Qué corre |
|---|---|---|
| 1 | INICIAR | `down` + `up -d --build` |
| 2 | CERRAR Y CONSERVAR MIS PROYECTOS | `down` |
| 3 | CERRAR Y BORRAR TODO | `down -v` |
| 4 | SALIR | — |

Dos decisiones de seguridad: la opción destructiva **no está pegada a la
habitual** (para que un dedo apurado no la toque) y **pide escribir la palabra
`BORRAR`** en vez de un «s/n», que se contesta por reflejo. Del otro lado hay
trabajo que no se puede recuperar.

El `-v` borra los volúmenes nombrados (`core_data`, `blast_data`), que es donde
viven las bases y los archivos. **No toca imágenes** ni nada fuera del proyecto:
si borrara las imágenes, el siguiente arranque tendría que reconstruirlo todo y
tardaría lo mismo que una instalación desde cero.

Las instrucciones del cliente siguen siendo válidas sin tocarlas: dicen «escribe
la Opción 2 para apagar», y la opción 2 sigue siendo apagar.

## Con qué vista arranca el paso 4

`--vista nueva` (por defecto) o `--vista anterior`. El script lo escribe como
`VITE_VISTA_NUEVA` en el `.env` del paquete, y Vite lo resuelve en tiempo de
build, así que queda fijado en el bundle.

Se escribe **explícitamente** aunque el código ya arranque con la nueva cuando
la variable no está (`undefined !== "false"`): eso es funcionar por ausencia, y
bastaría con que alguien agregue la variable con otro valor para que la entrega
cambie de vista sin que nadie lo haya decidido.

Con la vista nueva, el usuario conserva el botón «Ver con la vista anterior»
para volver a la de siempre sin reconstruir nada.

## Qué hay acá

| Archivo | Qué es |
|---|---|
| `docker-compose.yml` | **El compose canónico.** El que se copia al paquete |
| `detovision.bat` | El menú que abre el usuario. **Modificado en el v8** (ver abajo) |
| `armar_paquete.py` | Genera el paquete completo desde el workspace |
| `instrucciones_cliente.txt` | El README que va en `Intrucciones/`, tal como se entregó |

## La forma del paquete

    Detovision/
        detovision.bat           menú: iniciar / detener / salir
        Dependencias/            instalador de Docker Desktop     (no se genera)
        Intrucciones/            dos videos y el README           (no se genera)
        mvp/
            docker-compose.yml
            flyRocks_frontend/
            flyrocks_core/
            flyrocks_blast_detector/

`Dependencias/` e `Intrucciones/` pesan cientos de MB y no cambian: se copian
del paquete anterior con `--extras <ruta>`. Si no se pasa, el script lo avisa y
deja el paquete sin ellas.

## Lo que el script decide por ti

**No viaja `flyrocks_core/debug/`.** Ahí están nuestros casos congelados, los
videos de cliente, los experimentos y las notas internas — varios GB, y cosas
que no corresponde mandar. El core no lo necesita: su Dockerfile solo copia
`pyproject.toml`, `uv.lock` y `src/`. Tampoco viajan `data/`, `temp_videos/`,
`*.db`, `.venv`, `node_modules` ni `dist/`: son estado local, y si viajaran el
cliente arrancaría con nuestros análisis dentro.

Medido: **13,7 MB** contra los varios GB del workspace.

**Sí viaja `flyRocks_frontend/public/vista.html`**, que es la vista del último
paso. Es una copia *generada* desde `flyrocks_core/debug/demo/vista.html`, así
que el script **republica antes de copiar**: si no, el paquete saldría con la
vista de la vuelta anterior y nadie se enteraría hasta que el cliente la abre.

**Sí viaja el `.env` del frontend.** Vite resuelve las `VITE_URL_*` en tiempo de
build, dentro del contenedor: sin ese archivo la aplicación se construye con las
URLs en `undefined` y no encuentra ningún backend. Los valores son los mismos
que ya usa el cliente (`localhost:8000` y `localhost:8009`).

## Las tres diferencias con el paquete v3

El `docker-compose.yml` de acá no es el que venía en `Detovision_V3/…/mvp/`.
Cambia en tres cosas, y las tres importan:

1. **Volúmenes nombrados** (`core_data`, `blast_data`). El `.bat` corre
   `docker compose down` **al iniciar y al detener**, y el arranque además usa
   `--build`. Sin volúmenes eso borraba las dos SQLite y todos los archivos en
   cada sesión: **el cliente empezaba de cero siempre**. Los volúmenes nombrados
   sobreviven a `down` — solo `down -v` los elimina, y el `.bat` no lo usa — así
   que la persistencia funciona **sin tocar el script ni las instrucciones**.
2. **`RETENCION_HORAS: 24`**. Antes eran 2 h fijas: lo que empezabas hoy no
   estaba mañana.
3. **`flyrocks_blast_detector`** en vez de `./flyrocks`, que es como se llamaba
   esa carpeta hasta la v3.

Y se suma `CACHE_MAX_MB` para que la caché por nodo no crezca sin límite en la
máquina del cliente.

## Comparado contra lo último que se entregó (Detovision_v7, 2026-07-23)

El andamiaje **no ha cambiado en cuatro versiones**: `detovision.bat`,
`Intrucciones/README.txt` y la estructura de carpetas son byte a byte idénticos
entre el v3 (abril) y el v7 (julio). Este paquete los conserva sin tocar. La
única pieza de andamiaje que cambia es el `docker-compose.yml`, por las tres
razones de arriba.

Del código: de los 98 archivos que ambos paquetes comparten, **84 son idénticos
y 14 cambian** (ignorando fin de línea; el v7 usa CRLF y el workspace LF, lo que
al comparar de forma ingenua hace parecer que cambió todo). Los 14 son
exactamente los tocados por P0, P1, P16 y P17, más tres archivos nuevos:
`public/vista.html`, `Step4Bifurcacion.tsx` y `utils/malla.py`.

### Lo que el v7 arrastra y este paquete no

Encontrado al comparar. Nada de esto hace falta para correr, y por eso el script
lo excluye:

| Qué | Cuánto | Por qué importa |
|---|---|---|
| Tres carpetas `.git` completas | ~17,6 MB | Historial entero, ramas, y `config` con las URLs de los repos **privados** (`Kauel-Chile/…`, `yeriel/flyrocks_core`). Sin credenciales — se verificó |
| 36 `__pycache__/*.pyc` | — | Bytecode, de dos versiones de Python distintas (3.11 y 3.13) |
| `flyrocks.db` y `video_analysis.db` | 796 KB + 12 KB | **Las bases SQLite de desarrollo**, con análisis nuestros dentro |
| `src/local.py`, `src/local_2.py` | — | Scripts locales que no son parte del producto |

**39 MB el v7 contra 14 MB este.**

## Probar el paquete antes de entregarlo

    cd entrega/salida/Detovision/mvp
    docker compose up -d --build

Levanta en `localhost:3000`. Es exactamente lo que hace la opción 1 del `.bat`,
así que si esto funciona, el `.bat` funciona.

## Pendiente

El compose de la raíz del workspace (`Enaex - Flyrocks/docker-compose.yml`)
quedó **duplicando** a este, con las mismas tres correcciones pero rutas
distintas (`./detovision_standalone/<servicio>`). Sirve para levantar el
proyecto en desarrollo, pero son dos archivos que van a divergir. Hay que
decidir si el de desarrollo se deriva de este o se elimina.
