"""phase 10 exposure engine daily snapshots

Revision ID: m0011
Revises: m0010
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0011"
down_revision: Union[str, None] = "m0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exposures_daily",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=False),
        sa.Column("date", sa.Text, nullable=False),
        sa.Column("directional", sa.Text, nullable=True),
        sa.Column("bond", sa.Text, nullable=True),
        sa.Column("structure_type", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=True),
        sa.Column("projection_version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "condition_id", "date"),
    )
    op.create_index(
        "ix_exposures_daily_wallet_date", "exposures_daily", ["wallet", "date"]
    )

    op.create_table(
        "event_exposures_daily",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("date", sa.Text, nullable=False),
        sa.Column("exposure_vector_json", sa.Text, nullable=False),
        sa.Column("net_after_exclusivity", sa.Text, nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "event_id", "date"),
    )
    op.create_index(
        "ix_event_exposures_daily_wallet_date",
        "event_exposures_daily",
        ["wallet", "date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_exposures_daily_wallet_date", table_name="event_exposures_daily"
    )
    op.drop_table("event_exposures_daily")
    op.drop_index("ix_exposures_daily_wallet_date", table_name="exposures_daily")
    op.drop_table("exposures_daily")
