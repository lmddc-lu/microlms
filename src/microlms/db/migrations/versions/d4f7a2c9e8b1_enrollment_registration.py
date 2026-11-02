"""enrollment xapi registration

Revision ID: d4f7a2c9e8b1
Revises: a5c8e2f7b9d1
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f7a2c9e8b1'
down_revision: Union[str, Sequence[str], None] = 'a5c8e2f7b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_enrollment', sa.Column('registration', sa.Uuid(), nullable=True))
    # Existing enrollments get a fresh registration; statements recorded before
    # this revision carry no registration and are not attributed to them
    op.execute('UPDATE lms_enrollment SET registration = gen_random_uuid()')
    op.alter_column('lms_enrollment', 'registration', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_enrollment', 'registration')
