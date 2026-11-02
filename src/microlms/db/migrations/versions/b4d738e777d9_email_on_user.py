"""email on user

Revision ID: b4d738e777d9
Revises: 3bafe8e306db
Create Date: 2026-10-22 12:00:00.000000

Makes the email a profile field of the user again (``lms_user.email``,
optional and unique), the reverse of ``d9c4b7e2f1a6``: every sign-in email
moves from ``lms_password_credential`` to its user, and the credential keeps
only the password and its lockout state. Credential rows still waiting for a
password are dropped, since a credential now exists only once a password is
set; their email stays on the user.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d738e777d9'
down_revision: Union[str, Sequence[str], None] = '3bafe8e306db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('lms_user', sa.Column('email', sa.String(length=320), nullable=True))
    op.create_unique_constraint(op.f('uq_lms_user_email'), 'lms_user', ['email'])
    op.execute(
        "UPDATE lms_user SET email = c.email FROM lms_password_credential c "
        "WHERE c.user_id = lms_user.id"
    )
    op.execute("DELETE FROM lms_password_credential WHERE hashed_password IS NULL")
    op.drop_constraint(
        op.f('uq_lms_password_credential_email'), 'lms_password_credential', type_='unique'
    )
    op.drop_column('lms_password_credential', 'email')
    op.alter_column(
        'lms_password_credential',
        'hashed_password',
        existing_type=sa.String(length=255),
        nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema. Users with an email and no password get a sign-in row
    waiting for one."""
    op.add_column(
        'lms_password_credential', sa.Column('email', sa.String(length=320), nullable=True)
    )
    op.alter_column(
        'lms_password_credential',
        'hashed_password',
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.execute(
        "UPDATE lms_password_credential SET email = u.email FROM lms_user u "
        "WHERE u.id = lms_password_credential.user_id"
    )
    op.execute("DELETE FROM lms_password_credential WHERE email IS NULL")
    op.execute(
        "INSERT INTO lms_password_credential (user_id, email, hashed_password, failed_attempts) "
        "SELECT u.id, u.email, NULL, 0 FROM lms_user u "
        "WHERE u.email IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM lms_password_credential c WHERE c.user_id = u.id)"
    )
    op.alter_column(
        'lms_password_credential', 'email', existing_type=sa.String(length=320), nullable=False
    )
    op.create_unique_constraint(
        op.f('uq_lms_password_credential_email'), 'lms_password_credential', ['email']
    )
    op.drop_constraint(op.f('uq_lms_user_email'), 'lms_user', type_='unique')
    op.drop_column('lms_user', 'email')
