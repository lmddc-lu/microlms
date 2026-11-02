"""account and course lifecycle

Users gain ``is_active`` (deactivated accounts are refused authentication) and
every foreign key onto ``lms_user`` becomes ON DELETE CASCADE so deleting a
user removes all rows keyed to them; the enrollment children of the content
modules cascade from ``lms_enrollment`` too, so the user cascade never leaves
dangling runtime data. Courses gain a lifecycle ``status`` (draft/published/
archived); existing courses are backfilled to published, matching their
behavior so far. Password credentials gain login-lockout state.

Revision ID: 7c2e5a1b9d40
Revises: f3a9c1d2b4e5
Create Date: 2026-07-10 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7c2e5a1b9d40'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d2b4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables holding a user_id foreign key, children of lms_user
USER_CHILD_TABLES = (
    'lms_user_role',
    'lms_enrollment',
    'lms_anonymous_token',
    'lms_password_credential',
    'lms_scorm_attempt',
    'lms_cmi5_session',
)

# Tables holding an enrollment_id foreign key, children of lms_enrollment
ENROLLMENT_CHILD_TABLES = ('lms_scorm_attempt', 'lms_cmi5_session')


def upgrade() -> None:
    """Upgrade schema."""
    # Server defaults only backfill the existing rows; the application sets
    # the values on insert, so the defaults are dropped again to match the
    # model metadata.
    op.add_column(
        'lms_user',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column('lms_user', 'is_active', server_default=None)

    op.execute("CREATE TYPE course_status AS ENUM ('DRAFT', 'PUBLISHED', 'ARCHIVED')")
    op.add_column(
        'lms_course',
        sa.Column(
            'status',
            postgresql.ENUM(
                'DRAFT', 'PUBLISHED', 'ARCHIVED', name='course_status', create_type=False
            ),
            nullable=False,
            # Courses created before the lifecycle existed behaved as published
            server_default='PUBLISHED',
        ),
    )
    op.alter_column('lms_course', 'status', server_default=None)

    op.add_column(
        'lms_password_credential',
        sa.Column('failed_attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    op.alter_column('lms_password_credential', 'failed_attempts', server_default=None)
    op.add_column(
        'lms_password_credential',
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    )

    for table in USER_CHILD_TABLES:
        name = f'fk_{table}_user_id_lms_user'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(
            op.f(name), table, 'lms_user', ['user_id'], ['id'], ondelete='CASCADE'
        )
    for table in ENROLLMENT_CHILD_TABLES:
        name = f'fk_{table}_enrollment_id_lms_enrollment'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(
            op.f(name), table, 'lms_enrollment', ['enrollment_id'], ['id'], ondelete='CASCADE'
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ENROLLMENT_CHILD_TABLES:
        name = f'fk_{table}_enrollment_id_lms_enrollment'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(op.f(name), table, 'lms_enrollment', ['enrollment_id'], ['id'])
    for table in USER_CHILD_TABLES:
        name = f'fk_{table}_user_id_lms_user'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(op.f(name), table, 'lms_user', ['user_id'], ['id'])

    op.drop_column('lms_password_credential', 'locked_until')
    op.drop_column('lms_password_credential', 'failed_attempts')
    op.drop_column('lms_course', 'status')
    op.execute('DROP TYPE course_status')
    op.drop_column('lms_user', 'is_active')
