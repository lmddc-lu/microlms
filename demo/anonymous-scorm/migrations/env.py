"""Alembic environment for the demo chain and the packaged core chain.

The demo owns no tables, so there is no metadata to autogenerate from: its
only revision seeds data the core needs.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from microlms import Settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return Settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode: emit SQL without a DB connection."""
    context.configure(
        url=_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode: connect and apply directly."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
