"""playground org

Revision ID: 4d5b105eb421
Revises:
Create Date: 2026-11-02

Seeds the ``playground`` organization anonymous visitors register into
(MICROLMS_REGISTRATION_ORG_SLUG): every org role is held inside an
organization, so registration needs it to exist. Depends on the core
revision that creates ``lms_org``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d5b105eb421"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("demo",)
depends_on: str | Sequence[str] | None = "8600dc611dc0"


def upgrade() -> None:
    op.execute(
        "INSERT INTO lms_org (slug, name) VALUES ('playground', 'Playground') "
        "ON CONFLICT (slug) DO NOTHING"
    )


def downgrade() -> None:
    """Fails while users or courses still reference the org."""
    op.execute("DELETE FROM lms_org WHERE slug = 'playground'")
