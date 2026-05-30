"""SQLAlchemy engine + session factory.

Single file because we don't need full app-style segregation. The DB URL
comes from settings (defaults to ./trading.db). Tables are created lazily
on first import — no Alembic migrations yet (would be overkill for the
~5 tables we have; revisit when the schema stabilizes).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import init as load_settings
from persistence.models import Base

_engine = None
_SessionLocal: sessionmaker | None = None


def _get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings, _ = load_settings()
        _engine = create_engine(settings.database_url, future=True)
        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Wrap a unit-of-work with commit/rollback. Use this for every write path."""
    _get_engine()
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
