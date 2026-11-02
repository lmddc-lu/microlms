"""user last login

Adds ``lms_user.last_login_at``, stamped on each successful login through an
auth backend's login endpoint. Existing accounts start with no value.

Revision ID: 3b8e1f5c7a2d
Revises: 1228cc2e04e7
Create Date: 2026-10-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b8e1f5c7a2d'
down_revision: Union[str, Sequence[str], None] = '1228cc2e04e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_user', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_user', 'last_login_at')
