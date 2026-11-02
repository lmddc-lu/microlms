"""oidc identities

Revision ID: 3bafe8e306db
Revises: b8e4f1c7a3d5
Create Date: 2026-10-22 12:00:00.000000

Adds ``lms_oidc_identity``: the OpenID Connect sign-in of a user, the
provider's issuer and the subject it knows the user by.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3bafe8e306db'
down_revision: Union[str, Sequence[str], None] = 'b8e4f1c7a3d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_oidc_identity',
        sa.Column('issuer', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['lms_user.id'],
            name=op.f('fk_lms_oidc_identity_user_id_lms_user'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('issuer', 'subject', name=op.f('pk_lms_oidc_identity')),
    )
    op.create_index(
        op.f('ix_lms_oidc_identity_user_id'), 'lms_oidc_identity', ['user_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_lms_oidc_identity_user_id'), table_name='lms_oidc_identity')
    op.drop_table('lms_oidc_identity')
