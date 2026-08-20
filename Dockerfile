FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Configuraciones de uv y Python
# PYTHONPATH=/app/src le dice a Python que trate la carpeta src como la raíz de los módulos
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    # Los wheels de OpenCV pesan ~70 MB cada uno (van dos: opencv-python y
    # opencv-contrib-python). Con el timeout por defecto de uv (30 s) una
    # conexion lenta corta la descarga a medias y el build falla entero con
    # "I/O operation failed during extraction", que parece un error de disco.
    # Importa porque el detovision.bat del cliente corre `up --build`: sin esto,
    # un hipo de red en su oficina tumba el despliegue completo.
    UV_HTTP_TIMEOUT=180

# IMPORTANTE: Instalamos las dependencias del sistema.
# Como tienes opencv-python en tu pyproject.toml, sin esto el contenedor fallaría.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 \
    libxext6 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Copiamos archivos de dependencias
COPY pyproject.toml uv.lock ./

# 2. Instalamos dependencias (caché)
RUN uv sync --frozen --no-install-project --no-dev

# 3. Copiamos TU código
COPY src ./src

# 3b. Los pesos del filtro de humo por IA. La carpeta viaja siempre (trae su
# README) aunque el .onnx no este: asi el build no depende de tener el modelo a
# mano, pero la imagen lo lleva cuando si esta. Sin el, el nodo 1.5 no puede
# cargar la sesion ONNX y el analisis falla.
COPY modelos ./modelos

# 4. Sincronizamos el proyecto final
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Llamamos a uvicorn directamente desde la ruta absoluta del entorno virtual.
# Nota: Al usar PYTHONPATH=/app/src, el módulo es "main:app", no "src.main:app"
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]