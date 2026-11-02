"""uuid identity and role set

DESTRUCTIVE: user identity moves from an int autoincrement to a uuid primary
key and no mapping between the two is kept, so every row referencing a user is
deleted before the column types change. The single ``role`` column on
``lms_user`` is replaced by the ``lms_user_role`` association table (one row
per held role), reusing the ``user_role`` enum type created by the initial
schema revision.

Revision ID: ae272c8fffda
Revises: bcd7940a4755
Create Date: 2026-07-10 12:10:15.446926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ae272c8fffda'
down_revision: Union[str, Sequence[str], None] = 'bcd7940a4755'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The enum type already exists from revision 9764faba9ac1; create_type=False
# stops SQLAlchemy from emitting a duplicate CREATE TYPE on table creation
USER_ROLE = postgresql.ENUM('LEARNER', 'INSTRUCTOR', 'ADMIN', name='user_role', create_type=False)

# Tables holding a user_id foreign key, children of lms_user
USER_FK_TABLES = (
    'lms_anonymous_token',
    'lms_password_credential',
    'lms_enrollment',
    'lms_scorm_attempt',
    'lms_cmi5_session',
)


def _delete_user_linked_rows() -> None:
    # Children first so foreign keys never dangle mid-transaction
    for table in ('lms_scorm_attempt', 'lms_cmi5_session', 'lms_anonymous_token',
                  'lms_password_credential', 'lms_enrollment', 'lms_user'):
        op.execute(f'DELETE FROM {table}')


def _drop_user_fks() -> None:
    for table in USER_FK_TABLES:
        op.drop_constraint(f'fk_{table}_user_id_lms_user', table, type_='foreignkey')


def _create_user_fks() -> None:
    for table in USER_FK_TABLES:
        op.create_foreign_key(
            op.f(f'fk_{table}_user_id_lms_user'), table, 'lms_user', ['user_id'], ['id']
        )


def _swap_user_id_columns(id_column: sa.Column, fk_type: sa.types.TypeEngine) -> None:
    """Drop and re-add the user primary key and every referencing column.

    int and uuid have no cast between them, so the columns are replaced rather
    than altered; dropping a column also drops its dependent constraints and,
    for the serial primary key, the owned sequence and default. The implicitly
    dropped constraints are recreated here or by the caller.
    """
    op.drop_column('lms_user', 'id')
    op.add_column('lms_user', id_column)
    op.create_primary_key(op.f('pk_lms_user'), 'lms_user', ['id'])

    for table in USER_FK_TABLES:
        op.drop_column(table, 'user_id')
        op.add_column(table, sa.Column('user_id', fk_type, nullable=False))

    # Dropping lms_password_credential.user_id dropped its primary key
    op.create_primary_key(
        op.f('pk_lms_password_credential'), 'lms_password_credential', ['user_id']
    )
    # Dropping lms_enrollment.user_id dropped the (user_id, course_id) unique
    op.create_unique_constraint(
        op.f('uq_lms_enrollment_user_id'), 'lms_enrollment', ['user_id', 'course_id']
    )


def upgrade() -> None:
    """Upgrade schema."""
    _delete_user_linked_rows()
    _drop_user_fks()
    _swap_user_id_columns(sa.Column('id', sa.Uuid(), nullable=False), sa.Uuid())
    _create_user_fks()

    op.create_table('lms_user_role',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', USER_ROLE, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['lms_user.id'], name=op.f('fk_lms_user_role_user_id_lms_user')),
    sa.PrimaryKeyConstraint('user_id', 'role', name=op.f('pk_lms_user_role'))
    )

    # The user_role enum type outlives the column and keeps backing lms_user_role
    op.drop_column('lms_user', 'role')


def downgrade() -> None:
    """Downgrade schema. Destructive best-effort: uuid ids cannot be mapped back."""
    op.add_column('lms_user', sa.Column('role', USER_ROLE, nullable=True))
    op.drop_table('lms_user_role')

    _delete_user_linked_rows()
    op.alter_column('lms_user', 'role', nullable=False)
    _drop_user_fks()
    # Identity restores the auto-generated ids of the original serial column
    _swap_user_id_columns(
        sa.Column('id', sa.Integer(), sa.Identity(), nullable=False), sa.Integer()
    )
    _create_user_fks()
