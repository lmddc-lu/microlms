"""Declarative base and session plumbing shared by all core models.

Client projects may declare their own tables against their own metadata and
reference core tables by foreign key, but must never alter core tables.

:func:`create_db_engine` and :func:`create_session_factory` are public,
re-exported from the ``microlms`` package: a client builds the app's engine
with them, passes it to ``create_app(engine=...)`` and shares it with its own
database work, and standalone scripts (no app running) build their own.
"""

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Deterministic constraint names so Alembic migrations stay portable
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all core ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UnsupportedDatabaseError(ValueError):
    """The database URL names a backend other than PostgreSQL."""


def create_db_engine(database_url: str, echo: bool) -> Engine:
    """Create the SQLAlchemy engine for the given PostgreSQL database URL.

    The engine owns the connection pool: build one per process and share it
    (see ``create_app(engine=...)``) rather than one per consumer.

    PostgreSQL is the only supported database (the core relies on its
    advisory locks, ``RETURNING`` and timezone-aware timestamps), so any
    other backend raises :class:`UnsupportedDatabaseError`.
    """
    backend = make_url(database_url).get_backend_name()
    if backend != "postgresql":
        raise UnsupportedDatabaseError(
            f"Database backend {backend!r} is not supported: microlms runs on PostgreSQL "
            "only (e.g. postgresql+psycopg://user:password@host/dbname)"
        )
    return create_engine(database_url, echo=echo)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the session factory bound to the given engine.

    Sessions keep their loaded attributes after commit, the same as the
    sessions the core hands to request handlers.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
