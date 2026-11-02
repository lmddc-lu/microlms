"""anonymous auth token

Revision ID: c7d8015d80e1
Revises: 9764faba9ac1
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8015d80e1'
down_revision: Union[str, Sequence[str], None] = '9764faba9ac1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_anonymous_token',
    sa.Column('token', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_anonymous_token_user_id_lms_user')),
    sa.PrimaryKeyConstraint('token', name=op.f('pk_lms_anonymous_token'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_anonymous_token')
