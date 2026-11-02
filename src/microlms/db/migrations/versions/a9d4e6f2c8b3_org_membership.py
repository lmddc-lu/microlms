"""org membership with per-org roles

Revision ID: a9d4e6f2c8b3
Revises: c3e9a5f7b1d2
Create Date: 2026-09-28 12:00:00.000000

Replaces the single ``lms_user.org_id`` with ``lms_org_membership``: a user
holds org roles inside any number of organizations, and ``lms_user_role``
keeps only the platform roles held outside any organization.

The access policy (which roles are platform roles) lives in client code, so
this migration cannot tell the two kinds apart. It copies every role of a
user who had an organization into a membership of that organization and
leaves ``lms_user_role`` untouched: a copied role that turns out to be a
platform role grants nothing as an org role, and a role left in
``lms_user_role`` that is an org role grants nothing there, so the copy never
widens anyone's access. Misplaced rows can be revoked through the API.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d4e6f2c8b3'
down_revision: Union[str, Sequence[str], None] = 'c3e9a5f7b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lms_org_membership',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['lms_org.id'], name=op.f('fk_lms_org_membership_org_id_lms_org')),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_org_membership_user_id_lms_user'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'org_id', 'role', name=op.f('pk_lms_org_membership'))
    )
    op.execute(
        "INSERT INTO lms_org_membership (user_id, org_id, role) "
        "SELECT u.id, u.org_id, r.role FROM lms_user u "
        "JOIN lms_user_role r ON r.user_id = u.id "
        "WHERE u.org_id IS NOT NULL"
    )
    op.drop_constraint(op.f('fk_lms_user_org_id_lms_org'), 'lms_user', type_='foreignkey')
    op.drop_column('lms_user', 'org_id')


def downgrade() -> None:
    """Downgrade schema. Best-effort: a user in several organizations keeps
    only the one with the lowest id, and every org role becomes a user role."""
    op.add_column('lms_user', sa.Column('org_id', sa.Integer(), nullable=True))
    op.create_foreign_key(op.f('fk_lms_user_org_id_lms_org'), 'lms_user', 'lms_org', ['org_id'], ['id'])
    op.execute(
        "UPDATE lms_user SET org_id = m.org_id FROM "
        "(SELECT user_id, MIN(org_id) AS org_id FROM lms_org_membership GROUP BY user_id) m "
        "WHERE m.user_id = lms_user.id"
    )
    op.execute(
        "INSERT INTO lms_user_role (user_id, role) "
        "SELECT DISTINCT user_id, role FROM lms_org_membership "
        "ON CONFLICT DO NOTHING"
    )
    op.drop_table('lms_org_membership')
