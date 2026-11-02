"""course owning org

Courses gain an owning organization (``lms_course.owner_org_id``, nullable):
org-scoped authors act only on courses their org owns, while None means
platform-owned (only platform-scoped authors touch it). Ownership is separate
from the visibility assignments in ``lms_course_org``. Existing courses stay
platform-owned, matching their behavior so far.

Revision ID: b2c7f4a8d3e6
Revises: f8b3d6c2a9e4
Create Date: 2026-08-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c7f4a8d3e6'
down_revision: Union[str, Sequence[str], None] = 'f8b3d6c2a9e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_course', sa.Column('owner_org_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f('fk_lms_course_owner_org_id_lms_org'),
        'lms_course',
        'lms_org',
        ['owner_org_id'],
        ['id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('fk_lms_course_owner_org_id_lms_org'), 'lms_course', type_='foreignkey')
    op.drop_column('lms_course', 'owner_org_id')
