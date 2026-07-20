FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Configuraciones de uv y Python
# PYTHONPATH=/app/src le dice a Python que trate la carpeta src como la raíz de los módulos
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

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

# 4. Sincronizamos el proyecto final
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Llamamos a uvicorn directamente desde la ruta absoluta del entorno virtual.
# Nota: Al usar PYTHONPATH=/app/src, el módulo es "main:app", no "src.main:app"
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]