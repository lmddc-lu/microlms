"""organizations

Adds the optional multi-tenancy layer: the ``lms_org`` table, a nullable
``org_id`` membership column on ``lms_user`` (NULL = global user), the
``lms_course_org`` visibility assignments, and the ``MANAGER`` value in the
``user_role`` enum. Upgrade is non-destructive; the downgrade deletes
manager role rows because Postgres cannot remove a value from an enum type.

Revision ID: 8600dc611dc0
Revises: ae272c8fffda
Create Date: 2026-07-10 13:55:40.037493

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8600dc611dc0'
down_revision: Union[str, Sequence[str], None] = 'ae272c8fffda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres >= 12 allows ADD VALUE inside a transaction as long as the new
    # value is not used by the same transaction; this migration only adds it.
    # IF NOT EXISTS makes a re-run after a partial failure harmless. Alembic
    # autogenerate never compares enum members, so this is hand-written.
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'MANAGER'")

    op.create_table('lms_org',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_org')),
    sa.UniqueConstraint('slug', name=op.f('uq_lms_org_slug'))
    )
    op.create_table('lms_course_org',
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['lms_course.id'], name=op.f('fk_lms_course_org_course_id_lms_course')),
    sa.ForeignKeyConstraint(['org_id'], ['lms_org.id'], name=op.f('fk_lms_course_org_org_id_lms_org')),
    sa.PrimaryKeyConstraint('course_id', 'org_id', name=op.f('pk_lms_course_org'))
    )
    op.add_column('lms_user', sa.Column('org_id', sa.Integer(), nullable=True))
    op.create_foreign_key(op.f('fk_lms_user_org_id_lms_org'), 'lms_user', 'lms_org', ['org_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema. Best-effort: MANAGER role rows are deleted."""
    op.drop_constraint(op.f('fk_lms_user_org_id_lms_org'), 'lms_user', type_='foreignkey')
    op.drop_column('lms_user', 'org_id')
    op.drop_table('lms_course_org')
    op.drop_table('lms_org')
    # Postgres cannot remove a value from an enum type: MANAGER stays in
    # user_role as harmless residue. Delete the rows using it so the data
    # matches the pre-manager models; a user whose only role was manager
    # ends up role-less and must be re-granted one.
    op.execute("DELETE FROM lms_user_role WHERE role = 'MANAGER'")
