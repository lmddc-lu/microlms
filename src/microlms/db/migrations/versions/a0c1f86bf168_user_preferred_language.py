"""user preferred language

Adds ``lms_user.lang``, the user's preferred language as a BCP 47 tag.
Existing accounts start with no language chosen.

Revision ID: a0c1f86bf168
Revises: 45f112c8c5f4
Create Date: 2026-10-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a0c1f86bf168'
down_revision: Union[str, Sequence[str], None] = '45f112c8c5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_user', sa.Column('lang', sa.String(length=35), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_user', 'lang')
