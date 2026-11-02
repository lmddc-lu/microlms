"""lti module tables

Revision ID: 45671404345b
Revises: e6b2d8f4a1c7
Create Date: 2026-09-28 15:46:18.474792

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45671404345b'
down_revision: Union[str, Sequence[str], None] = 'e6b2d8f4a1c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_lti_tool',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('client_id', sa.String(length=255), nullable=False),
    sa.Column('deployment_id', sa.String(length=255), nullable=False),
    sa.Column('oidc_login_url', sa.String(length=2000), nullable=False),
    sa.Column('redirect_uris', sa.JSON(), nullable=False),
    sa.Column('jwks_url', sa.String(length=2000), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_lti_tool')),
    sa.UniqueConstraint('client_id', name=op.f('uq_lms_lti_tool_client_id'))
    )
    op.create_table('lms_lti_client_assertion',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tool_id', sa.Integer(), nullable=False),
    sa.Column('jti', sa.String(length=255), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tool_id'], ['lms_lti_tool.id'], name=op.f('fk_lms_lti_client_assertion_tool_id_lms_lti_tool')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_lti_client_assertion')),
    sa.UniqueConstraint('tool_id', 'jti', name=op.f('uq_lms_lti_client_assertion_tool_id'))
    )
    op.create_table('lms_lti_link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('variant_id', sa.Integer(), nullable=False),
    sa.Column('tool_id', sa.Integer(), nullable=False),
    sa.Column('resource_link_id', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('target_link_uri', sa.String(length=2000), nullable=False),
    sa.Column('custom', sa.JSON(), nullable=False),
    sa.Column('score_maximum', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['tool_id'], ['lms_lti_tool.id'], name=op.f('fk_lms_lti_link_tool_id_lms_lti_tool')),
    sa.ForeignKeyConstraint(['variant_id'], ['lms_activity_variant.id'], name=op.f('fk_lms_lti_link_variant_id_lms_activity_variant')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_lti_link')),
    sa.UniqueConstraint('resource_link_id', name=op.f('uq_lms_lti_link_resource_link_id'))
    )
    op.create_table('lms_lti_launch',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_hint', sa.String(length=200), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['link_id'], ['lms_lti_link.id'], name=op.f('fk_lms_lti_launch_link_id_lms_lti_link')),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_lti_launch_user_id_lms_user'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_lti_launch'))
    )
    op.create_index(op.f('ix_lms_lti_launch_message_hint'), 'lms_lti_launch', ['message_hint'], unique=True)
    op.create_table('lms_lti_result',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('enrollment_id', sa.Integer(), nullable=False),
    sa.Column('score_given', sa.Float(), nullable=True),
    sa.Column('score_maximum', sa.Float(), nullable=True),
    sa.Column('activity_progress', sa.String(length=20), nullable=False),
    sa.Column('grading_progress', sa.String(length=20), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['enrollment_id'], ['lms_enrollment.id'], name=op.f('fk_lms_lti_result_enrollment_id_lms_enrollment'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['link_id'], ['lms_lti_link.id'], name=op.f('fk_lms_lti_result_link_id_lms_lti_link')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_lti_result')),
    sa.UniqueConstraint('link_id', 'enrollment_id', name=op.f('uq_lms_lti_result_link_id'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_lti_result')
    op.drop_index(op.f('ix_lms_lti_launch_message_hint'), table_name='lms_lti_launch')
    op.drop_table('lms_lti_launch')
    op.drop_table('lms_lti_link')
    op.drop_table('lms_lti_client_assertion')
    op.drop_table('lms_lti_tool')
