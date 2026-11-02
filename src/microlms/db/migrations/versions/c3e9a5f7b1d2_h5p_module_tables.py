"""h5p module tables

Revision ID: c3e9a5f7b1d2
Revises: b2c7f4a8d3e6
Create Date: 2026-08-26 18:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e9a5f7b1d2'
down_revision: Union[str, Sequence[str], None] = 'b2c7f4a8d3e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_h5p_content',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('variant_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('main_library', sa.String(length=200), nullable=False),
    sa.Column('activity_iri', sa.String(length=500), nullable=False),
    sa.Column('storage_prefix', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['variant_id'], ['lms_activity_variant.id'], name=op.f('fk_lms_h5p_content_variant_id_lms_activity_variant')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_h5p_content'))
    )
    op.create_table('lms_h5p_attempt',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('content_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('enrollment_id', sa.Integer(), nullable=False),
    sa.Column('auth_token', sa.String(length=200), nullable=False),
    sa.Column('satisfied', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['lms_h5p_content.id'], name=op.f('fk_lms_h5p_attempt_content_id_lms_h5p_content')),
    sa.ForeignKeyConstraint(['enrollment_id'], ['lms_enrollment.id'], ondelete='CASCADE', name=op.f('fk_lms_h5p_attempt_enrollment_id_lms_enrollment')),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], ondelete='CASCADE', name=op.f('fk_lms_h5p_attempt_user_id_lms_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_h5p_attempt')),
    sa.UniqueConstraint('content_id', 'enrollment_id', name=op.f('uq_lms_h5p_attempt_content_id'))
    )
    op.create_index(op.f('ix_lms_h5p_attempt_auth_token'), 'lms_h5p_attempt', ['auth_token'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_lms_h5p_attempt_auth_token'), table_name='lms_h5p_attempt')
    op.drop_table('lms_h5p_attempt')
    op.drop_table('lms_h5p_content')
