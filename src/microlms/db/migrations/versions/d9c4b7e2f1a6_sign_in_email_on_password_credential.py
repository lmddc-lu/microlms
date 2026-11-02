"""sign-in email on the password credential

Revision ID: d9c4b7e2f1a6
Revises: a0c1f86bf168
Create Date: 2026-10-15 12:00:00.000000

Moves the email from ``lms_user`` onto ``lms_password_credential``: the email
is the sign-in identifier of the email/password method, not an attribute of
every user. ``hashed_password`` becomes nullable, so an account invited with
an email has its sign-in row before it chooses a password.

Every user with an email keeps it as their sign-in email (with no password
yet when they had none). A credential of a user without an email is dropped:
it had no identifier to log in with.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9c4b7e2f1a6'
down_revision: Union[str, Sequence[str], None] = 'a0c1f86bf168'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema. Sign-in rows still waiting for a password are
    dropped, their email kept on the user."""
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
