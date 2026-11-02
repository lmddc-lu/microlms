"""launch credential expiry

Revision ID: b8e4f1c7a3d5
Revises: d9c4b7e2f1a6
Create Date: 2026-10-21 12:00:00.000000

Gives every content launch credential an end: ``token_expires_at`` on the
SCORM and H5P attempts (their player token) and ``expires_at`` on the cmi5
sessions. Rows launched before this revision expire immediately, so open
players stop and learners launch again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e4f1c7a3d5'
down_revision: Union[str, Sequence[str], None] = 'd9c4b7e2f1a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('lms_scorm_attempt', 'token_expires_at'),
    ('lms_h5p_attempt', 'token_expires_at'),
    ('lms_cmi5_session', 'expires_at'),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in _COLUMNS:
        # The default only fills existing rows; the app always sets the value
        op.add_column(
            table,
            sa.Column(
                column, sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')
            ),
        )
        op.alter_column(table, column, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for table, column in _COLUMNS:
        op.drop_column(table, column)
