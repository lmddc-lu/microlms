"""password reset tokens

Adds ``lms_password_reset_token``: single-use password reset and invitation
tokens stored server-side as SHA-256 digests. Reset links issued before this
revision were stateless JWTs and stop working once it is applied.

Revision ID: e6b2d8f4a1c7
Revises: a9d4e6f2c8b3
Create Date: 2026-09-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6b2d8f4a1c7'
down_revision: Union[str, Sequence[str], None] = 'a9d4e6f2c8b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_password_reset_token',
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['lms_user.id'],
            name=op.f('fk_lms_password_reset_token_user_id_lms_user'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_lms_password_reset_token')),
    )
    op.create_index(
        op.f('ix_lms_password_reset_token_user_id'),
        'lms_password_reset_token',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_lms_password_reset_token_user_id'), table_name='lms_password_reset_token'
    )
    op.drop_table('lms_password_reset_token')
