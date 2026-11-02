"""lti tool identity sharing

Revision ID: 1fe9f7ee8780
Revises: 45671404345b
Create Date: 2026-09-28 16:47:37.909051

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1fe9f7ee8780'
down_revision: Union[str, Sequence[str], None] = '45671404345b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Tools registered before this revision keep sharing nothing but the user
    # id; the default only fills existing rows and is dropped afterwards
    op.add_column('lms_lti_tool', sa.Column('share_identity', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column('lms_lti_tool', 'share_identity', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_lti_tool', 'share_identity')
