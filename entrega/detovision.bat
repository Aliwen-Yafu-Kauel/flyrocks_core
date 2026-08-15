@echo off
:: Asegura que el script use la carpeta donde esta guardado como base
cd /d "%~dp0"

setlocal enabledelayedexpansion
title Gestor del Proyecto FlyRocks
color 0A

:: Variables de configuracion (Actualizado a la nueva carpeta)
set "CODE_DIR=mvp"

:MENU
cls
echo =======================================================
echo         GESTOR DEL PROYECTO FLYROCKS (Docker)
echo =======================================================
echo.
echo   [1] INICIAR
echo       Levanta la aplicacion y abre el navegador.
echo       La primera vez la instala, asi que tarda unos minutos.
echo.
echo   [2] CERRAR Y CONSERVAR MIS PROYECTOS
echo       Apaga la aplicacion y libera memoria y CPU.
echo       Tus videos y analisis quedan guardados para la proxima vez.
echo.
echo   [3] CERRAR Y BORRAR TODO
echo       Apaga la aplicacion y ELIMINA los videos y analisis
echo       guardados. Esto no se puede deshacer.
echo.
echo   [4] SALIR
echo.
echo =======================================================
set /p opcion="Selecciona una opcion [1-4]: "

if "%opcion%"=="1" goto INICIAR_PROYECTO
if "%opcion%"=="2" goto DETENER_PROYECTO
if "%opcion%"=="3" goto BORRAR_TODO
if "%opcion%"=="4" goto SALIR

echo.
echo [!] Opcion no valida.
timeout /t 2 >nul
goto MENU


:INICIAR_PROYECTO
cls
echo =======================================================
echo             INICIANDO EL PROYECTO...
echo =======================================================
echo.

:: 1. Verificar si docker existe en el sistema
echo [1/5] Verificando instalacion de Docker...
docker --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] No se detecto Docker instalado. 
    echo Por favor, instala "Docker Desktop" manualmente antes de usar este script.
    pause
    goto MENU
)
echo [OK] Docker detectado.

:: 2. Verificar si el motor de Docker esta encendido
echo [2/5] Comprobando si Docker Desktop esta abierto...
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [AVISO] El motor de Docker esta apagado. Intentando iniciar Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo [INFO] Esperando 20 segundos a que el motor arranque...
    timeout /t 20 /nobreak >nul
) else (
    echo [OK] El motor de Docker esta activo.
)

:: 3. Entrar a la carpeta y limpiar ejecuciones viejas
echo [3/5] Accediendo a la carpeta "%CODE_DIR%" y limpiando...
if not exist "%CODE_DIR%" (
    echo [ERROR] No se encontro la carpeta "%CODE_DIR%".
    pause
    goto MENU
)
cd %CODE_DIR%
docker compose down >nul 2>&1

:: 4. Levantar contenedores
echo [4/5] Construyendo y levantando contenedores...
echo       (Esto puede tardar unos minutos la primera vez)
docker compose up -d --build

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Hubo un problema al levantar los contenedores.
    echo Revisa que no haya otro programa usando los puertos 3000, 8000 u 8009.
    cd ..
    pause
    goto MENU
)

:: 5. Abrir navegador
echo [5/5] Proyecto listo. Abriendo navegador...
timeout /t 5 /nobreak >nul
start http://localhost:3000

echo.
echo [EXITO] FlyRocks esta corriendo en http://localhost:3000
cd ..
echo Presiona cualquier tecla para volver al menu...
pause >nul
goto MENU


:DETENER_PROYECTO
cls
echo =======================================================
echo             DETENIENDO EL PROYECTO...
echo =======================================================
echo.

if not exist "%CODE_DIR%" (
    echo [ERROR] No se encuentra la carpeta "%CODE_DIR%".
    pause
    goto MENU
)

cd %CODE_DIR%
echo [INFO] Apagando contenedores y liberando recursos...
:: Sin -v a proposito: `down` a secas NO toca los volumenes nombrados, asi que
:: los videos y analisis del usuario sobreviven. Borrarlos es la opcion 3, y
:: tiene que ser una decision explicita.
docker compose down

echo.
echo [OK] Todos los servicios se han detenido correctamente.
echo [OK] Tus videos y analisis quedaron guardados.
cd ..
echo.
echo Presiona cualquier tecla para volver al menu...
pause >nul
goto MENU


:BORRAR_TODO
cls
echo =======================================================
echo           CERRAR Y BORRAR TODO
echo =======================================================
echo.
echo   Esto apaga la aplicacion y ELIMINA de tu equipo:
echo.
echo     - los videos que subiste
echo     - los analisis y reportes generados
echo     - el historial de trabajos
echo.
echo   NO se puede deshacer. Si solo quieres liberar memoria
echo   y CPU, usa la opcion 2 del menu.
echo.
echo =======================================================
echo.
:: Se pide escribir una palabra, no un "s/n": una confirmacion de una tecla se
:: contesta por reflejo, y del otro lado hay trabajo que no se puede recuperar.
set "confirmar="
set /p confirmar="Escribe BORRAR y presiona Enter para continuar: "

if /i not "%confirmar%"=="BORRAR" (
    echo.
    echo [INFO] Cancelado. No se borro nada.
    timeout /t 3 >nul
    goto MENU
)

echo.
if not exist "%CODE_DIR%" (
    echo [ERROR] No se encuentra la carpeta "%CODE_DIR%".
    pause
    goto MENU
)

cd %CODE_DIR%
echo [INFO] Apagando contenedores y borrando los datos guardados...
:: -v elimina los volumenes nombrados (core_data y blast_data), que es donde
:: viven las bases de datos y los archivos. Es lo unico que borra este script:
:: no toca imagenes ni nada fuera del proyecto.
docker compose down -v

echo.
echo [OK] Aplicacion apagada y datos eliminados.
echo [OK] La proxima vez que inicies, todo empieza limpio.
cd ..
echo.
echo Presiona cualquier tecla para volver al menu...
pause >nul
goto MENU


:SALIR
cls
echo Gracias por usar el gestor. Saliendo...
timeout /t 2 >nul
exit