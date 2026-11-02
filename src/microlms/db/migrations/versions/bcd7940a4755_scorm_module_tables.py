"""scorm module tables

Revision ID: bcd7940a4755
Revises: 4d0371cb3272
Create Date: 2026-07-09 17:56:20.592546

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bcd7940a4755'
down_revision: Union[str, Sequence[str], None] = '4d0371cb3272'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_scorm_sco',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('sco_index', sa.Integer(), nullable=False),
    sa.Column('identifier', sa.String(length=500), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('scorm_type', sa.Enum('SCO', 'ASSET', name='scorm_resource_type'), nullable=False),
    sa.Column('launch_url', sa.String(length=1000), nullable=False),
    sa.Column('activity_id', sa.String(length=500), nullable=False),
    sa.Column('course_activity_id', sa.String(length=500), nullable=False),
    sa.Column('storage_prefix', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['lms_course.id'], name=op.f('fk_lms_scorm_sco_course_id_lms_course')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_scorm_sco'))
    )
    op.create_table('lms_scorm_attempt',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('sco_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('enrollment_id', sa.Integer(), nullable=False),
    sa.Column('auth_token', sa.String(length=200), nullable=False),
    sa.Column('completion_status', sa.Enum('COMPLETED', 'INCOMPLETE', 'NOT_ATTEMPTED', 'UNKNOWN', name='scorm_completion_status'), nullable=False),
    sa.Column('success_status', sa.Enum('PASSED', 'FAILED', 'UNKNOWN', name='scorm_success_status'), nullable=False),
    sa.Column('score_scaled', sa.Float(), nullable=True),
    sa.Column('score_raw', sa.Float(), nullable=True),
    sa.Column('location', sa.String(length=1000), nullable=True),
    sa.Column('suspend_data', sa.Text(), nullable=True),
    sa.Column('total_time_seconds', sa.Float(), nullable=False),
    sa.Column('suspended', sa.Boolean(), nullable=False),
    sa.Column('satisfied', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['enrollment_id'], ['lms_enrollment.id'], name=op.f('fk_lms_scorm_attempt_enrollment_id_lms_enrollment')),
    sa.ForeignKeyConstraint(['sco_id'], ['lms_scorm_sco.id'], name=op.f('fk_lms_scorm_attempt_sco_id_lms_scorm_sco')),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_scorm_attempt_user_id_lms_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_scorm_attempt')),
    sa.UniqueConstraint('sco_id', 'enrollment_id', name=op.f('uq_lms_scorm_attempt_sco_id'))
    )
    op.create_index(op.f('ix_lms_scorm_attempt_auth_token'), 'lms_scorm_attempt', ['auth_token'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_lms_scorm_attempt_auth_token'), table_name='lms_scorm_attempt')
    op.drop_table('lms_scorm_attempt')
    op.drop_table('lms_scorm_sco')
