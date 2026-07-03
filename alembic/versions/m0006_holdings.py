"""holdings projection

Revision ID: m0006
Revises: m0005
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0006"
down_revision: Union[str, None] = "m0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "holdings",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        # Decimal strings, same rationale as wallet_events deltas.
        sa.Column("qty", sa.Text, nullable=False),
        # Per-share weighted-average cost of the current holding; "0" when flat.
        sa.Column("wac_cost", sa.Text, nullable=False),
        sa.Column("as_of_ts", sa.Integer, nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "token_id"),
    )


def downgrade() -> None:
    op.drop_table("holdings")
