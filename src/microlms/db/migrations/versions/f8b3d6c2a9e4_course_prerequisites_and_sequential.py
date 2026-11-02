"""course prerequisites and sequential courses

Courses gain prerequisite edges (``lms_course_prerequisite``: enrollment
refuses while a prerequisite course is uncompleted) and a ``sequential`` flag
(activities lock until every earlier one is satisfied). Existing courses stay
non-sequential with no prerequisites, matching their behavior so far.

Revision ID: f8b3d6c2a9e4
Revises: d4f7a2c9e8b1
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8b3d6c2a9e4'
down_revision: Union[str, Sequence[str], None] = 'd4f7a2c9e8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The server default only backfills existing rows; the application sets
    # the value on insert, so the default is dropped again to match the model
    op.add_column(
        'lms_course',
        sa.Column('sequential', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column('lms_course', 'sequential', server_default=None)

    op.create_table('lms_course_prerequisite',
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('prerequisite_course_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['lms_course.id'], name=op.f('fk_lms_course_prerequisite_course_id_lms_course')),
    sa.ForeignKeyConstraint(['prerequisite_course_id'], ['lms_course.id'], name=op.f('fk_lms_course_prerequisite_prerequisite_course_id_lms_course')),
    sa.PrimaryKeyConstraint('course_id', 'prerequisite_course_id', name=op.f('pk_lms_course_prerequisite'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_course_prerequisite')
    op.drop_column('lms_course', 'sequential')
