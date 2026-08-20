# Guía para la demo al cliente

> Preparada el 2026-08-16 para la presentación del 17. Todo lo de aquí quedó
> **verificado corriendo** en esta máquina, no es de memoria.

## Antes de empezar (5 minutos, no delante del cliente)

1. **Abre Docker Desktop** y espera a que el icono deje de moverse. Sin el motor
   encendido no arranca nada, y es el error más fácil de cometer con prisa.
2. Doble clic en `Detovision\detovision.bat` → **opción 1**.
3. Espera a que diga `[EXITO]` y abra el navegador en http://localhost:3000.

Las imágenes ya están construidas, así que arranca en segundos. La primera vez
tardaba minutos: eso ya está hecho.

**Comprobación rápida de que todo vive** (pégalo en una terminal):

    curl -s -o nul -w "front %{http_code}\n" http://localhost:3000/
    curl -s -o nul -w "core  %{http_code}\n" http://localhost:8009/docs
    curl -s -o nul -w "blast %{http_code}\n" http://localhost:8000/docs

Los tres tienen que responder `200`.

## Mostrar el FLUJO COMPLETO

http://localhost:3000 — el wizard de 5 pasos, con un video real.

El paso 4 arranca directo en **nuestra vista** (`VITE_VISTA_NUEVA=true` está
fijado en el `.env` del paquete), y tiene botón para volver a la vista anterior
si el cliente quiere comparar.

**Cuidado con los tiempos**: el análisis del pipeline tarda unos **2 minutos**
con el clip de referencia, más lo que demore subir el video. Si vas a correrlo
en vivo, cuéntalo como parte del relato («esto es lo que tarda el detector») en
vez de esperar en silencio.

## Mostrar SOLO el último paso

Hay dos caminos. El primero es el que usarías delante del cliente:

### a) Dentro de la app, con un análisis ya hecho

**Esta es la URL, ya con el análisis pre-cargado** (cópiala tal cual):

    http://localhost:3000/vista.html?job=b82a7c65-eb23-4a6b-bcac-fbea96527af2&api=http://localhost:8009

Ese análisis se corrió y se verificó el 2026-08-16 sobre el clip de referencia:
**726 trayectorias, 113 pozos de la malla y ancla temporal de 48 frames**, o sea
con el calce temporal funcionando. Los volúmenes de Docker persisten entre
sesiones, así que sigue ahí aunque cierres con la opción 2.

Abre al instante: no vuelve a procesar nada. El job id también quedó en
`debug/out/ultimo_job.txt` por si lo necesitas.

### b) La vista de taller, sin Docker

    uv run python debug/caso_serve.py
    → http://localhost:8770/demo/vista.html

Carga el caso congelado 3160-789 con todo: trayectorias, malla de tiros,
asociación al pozo y calce temporal. No depende de la app ni de Docker, así que
también sirve de **plan B** si Docker falla en el momento.

## El fondo de video

**No está en la app.** La vista que viaja en el paquete es exactamente la que se
probó, y no incluye esa capa. El fondo de video (clip frame a frame bajo los
trazos, con play/pausa y ±1 frame) vive **solo en la vista de taller**, la del
punto (b), y ahí funciona con el clip completo en 4K.

Si lo vas a mostrar, ábrelo aparte en el 8770 y preséntalo como lo que es: lo
que viene, no lo entregado.

## Si algo falla

| Síntoma | Qué hacer |
|---|---|
| El `.bat` dice que no detecta Docker | Docker Desktop no está abierto. Ábrelo y espera a que el icono se estabilice. |
| Error de puertos 3000 / 8000 / 8009 | Algo más los está usando. `docker ps` para ver qué hay arriba. |
| El paso 4 sale en blanco | Estás sin `?job=`. Usa la URL completa de la sección (a). |
| El análisis no termina | Mira los logs: `docker compose logs -f flyrocks-core` desde `Detovision\mvp`. |
| Todo se cayó y hay prisa | Plan B: la vista de taller del punto (b). No necesita Docker. |

## Al terminar

`detovision.bat` → **opción 2** (cierra y conserva). Tus análisis quedan
guardados para la próxima.

**No uses la opción 3**: borra los videos y análisis guardados, incluido el
análisis pre-cargado de esta guía. Pide escribir `BORRAR` justamente por eso.
