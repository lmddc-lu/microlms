"""The core migration chain."""

from alembic import command
from alembic.config import Config


def test_migrations_match_models(template_alembic_config: Config) -> None:
    """The schema `alembic upgrade heads` builds is the one the models declare,
    so autogenerate has nothing left to write."""
    command.check(template_alembic_config)
