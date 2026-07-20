import uuid
from typing import Optional

from sqlmodel import Field, SQLModel, Session, create_engine
from sqlalchemy import Column, JSON  # <-- NUEVAS IMPORTACIONES

SQLITE_URL = "sqlite:///./flyrocks.db"

engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})

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