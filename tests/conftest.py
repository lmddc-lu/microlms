"""PostgreSQL test databases built by the core migration chain.

One ``postgres:17-alpine`` container (the image the demo deploys on) serves
the whole session, so running the tests needs Docker. The schema comes from
``alembic upgrade heads`` into a template database, never from
``Base.metadata.create_all``, so every test exercises the migrations a real
deployment applies; each test then gets its own copy of the template.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:17-alpine"
TEMPLATE_DATABASE = "microlms_template"


def _database_url(server_url: str, database: str) -> str:
    return make_url(server_url).set(database=database).render_as_string(hide_password=False)


def _alembic_config(database_url: str) -> Config:
    """Alembic config for the core chain against ``database_url``.

    Built without alembic.ini so the env does not reconfigure logging.
    """
    config = Config()
    config.set_main_option("script_location", "microlms:db/migrations")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session")
def postgres_server_url() -> Iterator[str]:
    """URL of the session's PostgreSQL server (its default database)."""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def admin_engine(postgres_server_url: str) -> Iterator[Engine]:
    """Autocommit engine for CREATE/DROP DATABASE, which refuse a transaction."""
    engine = create_engine(postgres_server_url, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def template_database_url(postgres_server_url: str, admin_engine: Engine) -> str:
    """A database migrated to ``heads``, cloned by every test."""
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{TEMPLATE_DATABASE}"'))
    url = _database_url(postgres_server_url, TEMPLATE_DATABASE)
    command.upgrade(_alembic_config(url), "heads")
    return url


@pytest.fixture
def template_alembic_config(template_database_url: str) -> Config:
    """Alembic config pointing at the migrated template database."""
    return _alembic_config(template_database_url)


@pytest.fixture
def database_url(
    postgres_server_url: str, admin_engine: Engine, template_database_url: str
) -> Iterator[str]:
    """URL of a fresh database holding the migrated schema, dropped after the test."""
    name = f"test_{uuid.uuid4().hex}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DATABASE}"'))
    yield _database_url(postgres_server_url, name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
