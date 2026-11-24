"""SQLAlchemy engine, session factory and declarative base."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DEFAULT_DATABASE_URL = "postgresql+psycopg2://postgres:postgres@localhost:5432/reeld"

# Stable constraint names so Alembic migrations stay deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def database_url() -> str:
    """DATABASE_URL from the environment, upgraded to an explicit SQLAlchemy driver.

    The legacy app uses a bare ``postgresql://`` URL with psycopg2. SQLAlchemy
    2.1 maps a bare URL to psycopg 3 instead, so pin psycopg2 here and the
    same variable keeps serving both apps.
    """
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def configure(url: str) -> Engine:
    """Point the suite at a specific database (used by tests and scripts)."""
    global _engine, _SessionLocal
    _engine = create_engine(url, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
