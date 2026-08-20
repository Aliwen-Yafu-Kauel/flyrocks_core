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

:: 2b. Comprobar que Docker tiene memoria suficiente.
::
:: El filtro de humo por IA necesita ~8 GB de pico, y WSL2 le da a Docker la
:: MITAD de la RAM del equipo por defecto. En un PC de 16 GB eso son 8 GB
:: compartidos con todo lo demas: el analisis muere a mitad de camino con
:: "exit 137" y parece que la aplicacion esta rota. Se avisa ANTES de empezar.
echo [2b/5] Comprobando memoria disponible para Docker...
set "RAM_GB="
for /f %%r in ('powershell -NoProfile -Command "$b=(Get-CimInstance Win32_ComputerSystem -EA SilentlyContinue).TotalPhysicalMemory; if(-not $b){$b=(Get-WmiObject Win32_ComputerSystem -EA SilentlyContinue).TotalPhysicalMemory}; if($b){[int]($b/1GB)}else{0}" 2^>nul') do set "RAM_GB=%%r"

if "%RAM_GB%"=="" set "RAM_GB=0"
if "%RAM_GB%"=="0" (
    echo [AVISO] No se pudo leer la memoria del equipo. Si el analisis se corta
    echo         solo, revisa el archivo de instrucciones: hace falta que Docker
    echo         tenga al menos 10 GB.
    goto :FIN_RAM
)
echo        Este equipo tiene %RAM_GB% GB de RAM.

if %RAM_GB% LSS 12 (
    echo.
    echo [AVISO] Con %RAM_GB% GB el analisis con IA puede no completarse.
    echo         Recomendado: 16 GB o mas. Puedes continuar, pero si el proceso
    echo         se corta solo, esa es la razon.
    echo.
    pause
    goto :FIN_RAM
)

:: Con 24 GB o mas, la mitad que WSL asigna por defecto ya supera los 10 GB.
if %RAM_GB% GEQ 24 (
    echo        [OK] La asignacion por defecto de Docker es suficiente.
    goto :FIN_RAM
)

findstr /I /C:"memory=10GB" "%USERPROFILE%\.wslconfig" >nul 2>&1
if %errorlevel%==0 (
    echo        [OK] Docker ya esta configurado con 10 GB.
    goto :FIN_RAM
)

echo.
echo        Docker necesita 10 GB y en este equipo tomaria solo la mitad de la
echo        RAM. Se puede configurar automaticamente; el archivo anterior, si
echo        existe, se guarda como .wslconfig.bak
echo.
set "AJUSTAR="
set /p AJUSTAR="        Configurar Docker con 10 GB ahora? (S/N): "
if /i not "%AJUSTAR%"=="S" (
    echo        Se continua sin cambiar nada.
    goto :FIN_RAM
)

if exist "%USERPROFILE%\.wslconfig" copy /Y "%USERPROFILE%\.wslconfig" "%USERPROFILE%\.wslconfig.bak" >nul
(
    echo [wsl2]
    echo memory=10GB
    echo swap=4GB
) > "%USERPROFILE%\.wslconfig"
echo        [OK] Configurado. Reiniciando Docker para aplicarlo...
wsl --shutdown >nul 2>&1
timeout /t 5 /nobreak >nul
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
echo        Esperando a que Docker vuelva (40 s)...
timeout /t 40 /nobreak >nul

:FIN_RAM
echo.

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