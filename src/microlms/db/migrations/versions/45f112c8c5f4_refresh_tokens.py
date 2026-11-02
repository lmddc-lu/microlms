"""refresh tokens

Adds ``lms_refresh_token``: the OAuth 2.0 refresh tokens of the JWT auth
backend, stored as SHA-256 digests and grouped by session (token family) for
rotation and reuse detection. Sessions start at the next password login.

Revision ID: 45f112c8c5f4
Revises: a84bddd204b9
Create Date: 2026-10-14 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45f112c8c5f4'
down_revision: Union[str, Sequence[str], None] = 'a84bddd204b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_refresh_token',
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('family_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('family_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['lms_user.id'],
            name=op.f('fk_lms_refresh_token_user_id_lms_user'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_lms_refresh_token')),
    )
    op.create_index(
        op.f('ix_lms_refresh_token_family_id'), 'lms_refresh_token', ['family_id'], unique=False
    )
    op.create_index(
        op.f('ix_lms_refresh_token_user_id'), 'lms_refresh_token', ['user_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_lms_refresh_token_user_id'), table_name='lms_refresh_token')
    op.drop_index(op.f('ix_lms_refresh_token_family_id'), table_name='lms_refresh_token')
    op.drop_table('lms_refresh_token')
