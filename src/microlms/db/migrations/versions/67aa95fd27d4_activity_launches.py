"""activity launches

Adds ``lms_activity_launch``: when each enrollment last launched each
activity, stamped by the content modules' launch endpoints. Launches made
before this revision are not recorded.

Revision ID: 67aa95fd27d4
Revises: 1fe9f7ee8780
Create Date: 2026-10-13 16:24:21.563373

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67aa95fd27d4'
down_revision: Union[str, Sequence[str], None] = '1fe9f7ee8780'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_activity_launch',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enrollment_id', sa.Integer(), nullable=False),
        sa.Column('activity_id', sa.Integer(), nullable=False),
        sa.Column('last_launched_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['activity_id'],
            ['lms_activity.id'],
            name=op.f('fk_lms_activity_launch_activity_id_lms_activity'),
        ),
        sa.ForeignKeyConstraint(
            ['enrollment_id'],
            ['lms_enrollment.id'],
            name=op.f('fk_lms_activity_launch_enrollment_id_lms_enrollment'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_activity_launch')),
        sa.UniqueConstraint(
            'enrollment_id', 'activity_id', name=op.f('uq_lms_activity_launch_enrollment_id')
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_activity_launch')
