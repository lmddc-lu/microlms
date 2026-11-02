"""audit log and enrollment due dates

Adds the ``lms_audit_log`` journal (``actor_id`` deliberately carries no
foreign key: the trail must survive user deletion, and externally provisioned
identities have no actor) and the enrollment deadline columns: ``due_at`` and
``due_notified_at``, the stamp that makes the due-soon notification fire once
per enrollment.

Revision ID: 3f8b2c6d1e57
Revises: 7c2e5a1b9d40
Create Date: 2026-07-10 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f8b2c6d1e57'
down_revision: Union[str, Sequence[str], None] = '7c2e5a1b9d40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lms_audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('impersonated_id', sa.Uuid(), nullable=True),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('target_type', sa.String(length=50), nullable=False),
        sa.Column('target_id', sa.String(length=100), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_lms_audit_log')),
    )
    op.create_index(op.f('ix_lms_audit_log_actor_id'), 'lms_audit_log', ['actor_id'])
    op.create_index(op.f('ix_lms_audit_log_target_id'), 'lms_audit_log', ['target_id'])

    op.add_column('lms_enrollment', sa.Column('due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'lms_enrollment', sa.Column('due_notified_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_enrollment', 'due_notified_at')
    op.drop_column('lms_enrollment', 'due_at')
    op.drop_index(op.f('ix_lms_audit_log_target_id'), table_name='lms_audit_log')
    op.drop_index(op.f('ix_lms_audit_log_actor_id'), table_name='lms_audit_log')
    op.drop_table('lms_audit_log')
