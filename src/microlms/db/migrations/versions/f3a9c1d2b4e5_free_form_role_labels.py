"""free-form role labels

The ``lms_user_role.role`` column moves from the fixed ``user_role`` enum to a
free-form varchar so a deployment can attach its own role labels (the
capability layer decides what each label grants, not this column). Existing
values are the enum member names (``LEARNER``, ...); they are lower-cased to the
labels the application now stores (``learner``, ...).

Revision ID: f3a9c1d2b4e5
Revises: 8600dc611dc0
Create Date: 2026-07-10 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d2b4e5'
down_revision: Union[str, Sequence[str], None] = '8600dc611dc0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'lms_user_role',
        'role',
        type_=sa.String(length=50),
        existing_nullable=False,
        postgresql_using='lower(role::text)',
    )
    # The enum type only backed this column, so it is no longer referenced
    op.execute('DROP TYPE user_role')


def downgrade() -> None:
    """Downgrade schema.

    Best-effort: any client-defined role label that is not a member of the
    recreated enum fails the cast, so downgrade only against the bundled roles.
    """
    op.execute("CREATE TYPE user_role AS ENUM ('LEARNER', 'INSTRUCTOR', 'MANAGER', 'ADMIN')")
    op.alter_column(
        'lms_user_role',
        'role',
        type_=postgresql.ENUM(
            'LEARNER', 'INSTRUCTOR', 'MANAGER', 'ADMIN', name='user_role', create_type=False
        ),
        existing_nullable=False,
        postgresql_using='upper(role)::user_role',
    )
