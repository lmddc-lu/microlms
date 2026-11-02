"""xapi ingest keys

Adds ``lms_xapi_ingest_key``: runtime-managed service keys for xAPI
ingestion, issued and revoked by platform managers alongside the statically
configured ``MICROLMS_XAPI_INGEST_KEYS``.

Revision ID: 8f3a5c2e6d91
Revises: 6a1f4e8b2c93
Create Date: 2026-07-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f3a5c2e6d91'
down_revision: Union[str, Sequence[str], None] = '6a1f4e8b2c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_xapi_ingest_key',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_xapi_ingest_key')),
        sa.UniqueConstraint('key', name=op.f('uq_lms_xapi_ingest_key_key')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('lms_xapi_ingest_key')
