"""password credential

Revision ID: b7e4c9a1f2d3
Revises: c7d8015d80e1
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4c9a1f2d3'
down_revision: Union[str, Sequence[str], None] = 'c7d8015d80e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_password_credential',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_password_credential_user_id_lms_user')),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_lms_password_credential'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_password_credential')
