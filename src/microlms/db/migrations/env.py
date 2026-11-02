"""Alembic environment for the core migration chain (branch label "core").

The database URL is taken from ``sqlalchemy.url`` when set in the Alembic
config, otherwise from the MICROLMS_DATABASE_URL environment variable.
Client projects add this chain to their own ``version_locations`` and run
``alembic upgrade heads`` to apply core and client migrations together.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Import model modules so their tables register on Base.metadata
import microlms.domain.activities  # noqa: F401
import microlms.domain.audit  # noqa: F401
import microlms.domain.auth_tokens  # noqa: F401
import microlms.domain.courses  # noqa: F401
import microlms.domain.enrollments  # noqa: F401
import microlms.domain.oidc_identities  # noqa: F401
import microlms.domain.orgs  # noqa: F401
import microlms.domain.password_credentials  # noqa: F401
import microlms.domain.refresh_tokens  # noqa: F401
import microlms.domain.users  # noqa: F401
import microlms.modules.cmi5.models  # noqa: F401
import microlms.modules.h5p.models  # noqa: F401
import microlms.modules.lti.models  # noqa: F401
import microlms.modules.scorm.models  # noqa: F401
import microlms.modules.xapi.keys  # noqa: F401
from microlms.config import Settings
from microlms.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return Settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode: emit SQL without a DB connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode: connect and apply directly."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
