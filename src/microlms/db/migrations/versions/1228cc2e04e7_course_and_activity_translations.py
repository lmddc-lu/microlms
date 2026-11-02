"""course and activity translations

Adds ``lms_course_translation`` (title and description per language) and
``lms_activity_translation`` (title per language): display texts kept next
to the reference ``title``/``description`` columns, which stay unchanged.

Revision ID: 1228cc2e04e7
Revises: 67aa95fd27d4
Create Date: 2026-10-13 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1228cc2e04e7'
down_revision: Union[str, Sequence[str], None] = '67aa95fd27d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_course_translation',
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('lang', sa.String(length=35), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ['course_id'],
            ['lms_course.id'],
            name=op.f('fk_lms_course_translation_course_id_lms_course'),
        ),
        sa.PrimaryKeyConstraint('course_id', 'lang', name=op.f('pk_lms_course_translation')),
    )
    op.create_table(
        'lms_activity_translation',
        sa.Column('activity_id', sa.Integer(), nullable=False),
        sa.Column('lang', sa.String(length=35), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(
            ['activity_id'],
            ['lms_activity.id'],
            name=op.f('fk_lms_activity_translation_activity_id_lms_activity'),
        ),
        sa.PrimaryKeyConstraint(
            'activity_id', 'lang', name=op.f('pk_lms_activity_translation')
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_activity_translation')
    op.drop_table('lms_course_translation')
