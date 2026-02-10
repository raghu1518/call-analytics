from __future__ import annotations

from contextlib import contextmanager
from sqlmodel import SQLModel, Session, create_engine

from app.config import settings


engine = create_engine(
    settings.resolved_database_url(),
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_schema()


def _ensure_schema() -> None:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(call)").fetchall()
        existing = {row[1] for row in rows}
        if "prompt_pack" not in existing:
            conn.exec_driver_sql("ALTER TABLE call ADD COLUMN prompt_pack TEXT")
        if "glossary_terms" not in existing:
            conn.exec_driver_sql("ALTER TABLE call ADD COLUMN glossary_terms TEXT")


@contextmanager
def get_session() -> Session:
    with Session(engine) as session:
        yield session
