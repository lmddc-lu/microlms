"""scorm package version

Adds ``version`` to ``lms_scorm_sco``: the SCORM edition (1.2 or 2004) the
package declared in its manifest, which decides the runtime API the player
exposes. Existing rows predate SCORM 1.2 support, so they are all 2004.

Revision ID: a5c8e2f7b9d1
Revises: e1b6d3a9c4f2
Create Date: 2026-07-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5c8e2f7b9d1'
down_revision: Union[str, Sequence[str], None] = 'e1b6d3a9c4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

scorm_version = sa.Enum('SCORM_12', 'SCORM_2004', name='scorm_version')


def upgrade() -> None:
    """Upgrade schema."""
    scorm_version.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'lms_scorm_sco',
        sa.Column('version', scorm_version, nullable=False, server_default='SCORM_2004'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('lms_scorm_sco', 'version')
    scorm_version.drop(op.get_bind(), checkfirst=True)
