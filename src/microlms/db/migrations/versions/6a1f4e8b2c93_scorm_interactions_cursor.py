"""scorm interactions cursor

Adds ``interactions_reported`` to ``lms_scorm_attempt``: the per-session
cursor into the cmi.interactions journal, so each answered interaction is
converted to a ``responded`` xAPI statement exactly once.

Revision ID: 6a1f4e8b2c93
Revises: 3f8b2c6d1e57
Create Date: 2026-07-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a1f4e8b2c93'
down_revision: Union[str, Sequence[str], None] = '3f8b2c6d1e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'lms_scorm_attempt',
        sa.Column('interactions_reported', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_scorm_attempt', 'interactions_reported')
