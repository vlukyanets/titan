"""added the sign-in time of devices

Revision ID: 966bcf545cfb
Revises: 684cad253b75
Create Date: 2026-10-03 11:37:27.311329
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "966bcf545cfb"
down_revision: str | None = "684cad253b75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "signed_in_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # Existing devices were signed in when they were paired.
    op.execute("UPDATE devices SET signed_in_at = created_at")


def downgrade() -> None:
    op.drop_column("devices", "signed_in_at")
