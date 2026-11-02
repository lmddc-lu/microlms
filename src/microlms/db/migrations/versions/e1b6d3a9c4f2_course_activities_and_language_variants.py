"""course activities and language variants

Revision ID: e1b6d3a9c4f2
Revises: 8f3a5c2e6d91
Create Date: 2026-07-24 10:00:00.000000

Content packages now attach to an activity variant instead of directly to a
course. Existing scorm/cmi5 content rows (and their attempts/sessions) are
deleted rather than migrated: packages must be re-imported into an activity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1b6d3a9c4f2'
down_revision: Union[str, Sequence[str], None] = '8f3a5c2e6d91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_activity',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['lms_course.id'], name=op.f('fk_lms_activity_course_id_lms_course')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_activity'))
    )
    op.create_table('lms_activity_variant',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('activity_id', sa.Integer(), nullable=False),
    sa.Column('lang', sa.String(length=35), nullable=False),
    sa.Column('module', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['activity_id'], ['lms_activity.id'], name=op.f('fk_lms_activity_variant_activity_id_lms_activity')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_activity_variant')),
    sa.UniqueConstraint('activity_id', 'lang', name=op.f('uq_lms_activity_variant_activity_id'))
    )
    op.create_table('lms_activity_progress',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enrollment_id', sa.Integer(), nullable=False),
    sa.Column('activity_id', sa.Integer(), nullable=False),
    sa.Column('variant_id', sa.Integer(), nullable=False),
    sa.Column('satisfied_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['activity_id'], ['lms_activity.id'], name=op.f('fk_lms_activity_progress_activity_id_lms_activity')),
    sa.ForeignKeyConstraint(['enrollment_id'], ['lms_enrollment.id'], ondelete='CASCADE', name=op.f('fk_lms_activity_progress_enrollment_id_lms_enrollment')),
    sa.ForeignKeyConstraint(['variant_id'], ['lms_activity_variant.id'], name=op.f('fk_lms_activity_progress_variant_id_lms_activity_variant')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_activity_progress')),
    sa.UniqueConstraint('enrollment_id', 'activity_id', name=op.f('uq_lms_activity_progress_enrollment_id'))
    )

    # Content rows cannot be mapped to variants that never existed; drop them
    # so the new non-nullable variant_id columns can be added. Packages are
    # re-imported into activities afterwards.
    op.execute('DELETE FROM lms_scorm_attempt')
    op.execute('DELETE FROM lms_scorm_sco')
    op.execute('DELETE FROM lms_cmi5_session')
    op.execute('DELETE FROM lms_cmi5_au')

    op.add_column('lms_scorm_sco', sa.Column('variant_id', sa.Integer(), nullable=False))
    op.create_foreign_key(op.f('fk_lms_scorm_sco_variant_id_lms_activity_variant'), 'lms_scorm_sco', 'lms_activity_variant', ['variant_id'], ['id'])
    op.drop_constraint(op.f('fk_lms_scorm_sco_course_id_lms_course'), 'lms_scorm_sco', type_='foreignkey')
    op.drop_column('lms_scorm_sco', 'course_id')

    op.add_column('lms_cmi5_au', sa.Column('variant_id', sa.Integer(), nullable=False))
    op.create_foreign_key(op.f('fk_lms_cmi5_au_variant_id_lms_activity_variant'), 'lms_cmi5_au', 'lms_activity_variant', ['variant_id'], ['id'])
    op.drop_constraint(op.f('fk_lms_cmi5_au_course_id_lms_course'), 'lms_cmi5_au', type_='foreignkey')
    op.drop_column('lms_cmi5_au', 'course_id')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DELETE FROM lms_cmi5_session')
    op.execute('DELETE FROM lms_cmi5_au')
    op.execute('DELETE FROM lms_scorm_attempt')
    op.execute('DELETE FROM lms_scorm_sco')

    op.add_column('lms_cmi5_au', sa.Column('course_id', sa.Integer(), nullable=False))
    op.create_foreign_key(op.f('fk_lms_cmi5_au_course_id_lms_course'), 'lms_cmi5_au', 'lms_course', ['course_id'], ['id'])
    op.drop_constraint(op.f('fk_lms_cmi5_au_variant_id_lms_activity_variant'), 'lms_cmi5_au', type_='foreignkey')
    op.drop_column('lms_cmi5_au', 'variant_id')

    op.add_column('lms_scorm_sco', sa.Column('course_id', sa.Integer(), nullable=False))
    op.create_foreign_key(op.f('fk_lms_scorm_sco_course_id_lms_course'), 'lms_scorm_sco', 'lms_course', ['course_id'], ['id'])
    op.drop_constraint(op.f('fk_lms_scorm_sco_variant_id_lms_activity_variant'), 'lms_scorm_sco', type_='foreignkey')
    op.drop_column('lms_scorm_sco', 'variant_id')

    op.drop_table('lms_activity_progress')
    op.drop_table('lms_activity_variant')
    op.drop_table('lms_activity')
