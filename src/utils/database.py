import os
import uuid
from typing import Optional

from sqlmodel import Field, SQLModel, Session, create_engine
from sqlalchemy import Column, JSON  # <-- NUEVAS IMPORTACIONES

# Todo lo que debe sobrevivir a un reinicio vive bajo DATA_DIR, y ese
# directorio es el unico que el docker-compose monta como volumen. Antes la
# base quedaba junto al codigo en /app, que no se puede montar sin tapar la
# aplicacion: por eso el `docker compose down` del script del cliente se
# llevaba por delante todos los trabajos.
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

SQLITE_URL = f"sqlite:///./{DATA_DIR}/flyrocks.db"

engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})


def migrar():
    """Agrega columnas nuevas a una tabla que ya existe.

    `SQLModel.metadata.create_all()` solo crea tablas que faltan: NO altera las
    existentes. Sin esto, al desplegar una version con un campo nuevo sobre una
    base ya creada, el core arranca bien y revienta recien al guardar un job
    ("table job has no column named ..."). En SQLite un ADD COLUMN es
    instantaneo y no reescribe la tabla, asi que sale gratis dejarlo corriendo
    en cada arranque.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("job"):
        return
    existentes = {c["name"] for c in insp.get_columns("job")}
    faltantes = {
        "entrada": "JSON",
        "json_data": "JSON",
    }
    with engine.begin() as con:
        for col, tipo in faltantes.items():
            if col not in existentes:
                con.execute(text(f"ALTER TABLE job ADD COLUMN {col} {tipo}"))
                print(f"[db] columna agregada: job.{col}")

class Job(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    is_running: bool = Field(default=True)
    status: str = Field(default="Preparando...")
    progress: int = Field(default=0) 
    result_file_path: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    
    # NUEVO: Campo para guardar el objeto JSON.
    # Usamos sa_column=Column(JSON) para decirle a la BD cómo tratarlo.
    json_data: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    # Con qué se corrió el análisis: homografía, zonas y parámetros.
    #
    # Antes esto llegaba como form-data, se usaba y se tiraba: terminado el
    # análisis, nadie podía reconstruir con qué homografía y qué zonas se hizo
    # — solo el navegador lo sabía, en memoria. Sin esto una vista no puede
    # dibujar la malla ni asociar trayectorias a partir de un `job_id`.
    #
    # Va como un solo JSON y no como seis columnas para poder sumarle campos
    # (el CSV de secuencia, por ejemplo) sin migrar la tabla cada vez.
    entrada: Optional[dict] = Field(default=None, sa_column=Column("entrada", JSON))
    
    @classmethod
    def update_status(cls, job_id: str, engine, **kwargs):
        """Método de clase para actualizar el estado de forma atómica."""
        with Session(engine) as session:
            db_job = session.get(cls, job_id)
            if db_job:
                for key, value in kwargs.items():
                    setattr(db_job, key, value)
                session.add(db_job)
                session.commit()
                session.refresh(db_job)
            return db_job