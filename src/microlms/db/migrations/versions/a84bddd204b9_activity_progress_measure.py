"""activity progress measure

Adds the progress measure (a fraction 0 to 1) content reports while it runs:
per enrollment and activity on ``lms_activity_launch``, per SCORM attempt on
``lms_scorm_attempt`` (cmi.progress_measure) and per cmi5 session on
``lms_cmi5_session`` (the cmi5 progress result extension). Existing rows
start with no value.

Revision ID: a84bddd204b9
Revises: 3b8e1f5c7a2d
Create Date: 2026-10-14 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a84bddd204b9'
down_revision: Union[str, Sequence[str], None] = '3b8e1f5c7a2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_activity_launch', sa.Column('progress_measure', sa.Float(), nullable=True))
    op.add_column('lms_scorm_attempt', sa.Column('progress_measure', sa.Float(), nullable=True))
    op.add_column('lms_cmi5_session', sa.Column('progress_measure', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_cmi5_session', 'progress_measure')
    op.drop_column('lms_scorm_attempt', 'progress_measure')
    op.drop_column('lms_activity_launch', 'progress_measure')
