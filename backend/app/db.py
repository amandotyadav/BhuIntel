"""Database engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine(url: str | None = None) -> None:
    """Re-create the engine (used by tests to point at the test database)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url or get_settings().database_url, pool_pre_ping=True, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def SessionLocal() -> Session:  # noqa: N802 - factory naming convention
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def db_ready() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            v = conn.execute(text("SELECT postgis_lib_version()")).scalar()
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return True, f"postgis {v}, migration {rev}"
    except Exception as exc:  # pragma: no cover - exercised by readiness probe
        return False, type(exc).__name__
