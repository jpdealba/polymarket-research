"""phase 22 inventory lifecycle simulation tables

Revision ID: m0029
Revises: m0028
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0029"
down_revision: Union[str, None] = "m0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "simulation_lifecycle_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("token_id", sa.Text, nullable=True),
        sa.Column("qty", sa.Text, nullable=False, server_default="0"),
        sa.Column("usdc_delta", sa.Text, nullable=False, server_default="0"),
        sa.Column("capital_released", sa.Text, nullable=False, server_default="0"),
        sa.Column("inventory_before_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("inventory_after_json", sa.Text, nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_simulation_lifecycle_events_run",
        "simulation_lifecycle_events",
        ["run_id", "ts"],
    )

    op.create_table(
        "simulation_lifecycle_summary",
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), primary_key=True),
        sa.Column("merge_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("merged_qty", sa.Text, nullable=False, server_default="0"),
        sa.Column("released_capital_total", sa.Text, nullable=False, server_default="0"),
        sa.Column("capital_recycled_total", sa.Text, nullable=False, server_default="0"),
        sa.Column("redeem_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("redeem_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("trading_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("merge_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("unresolved_inventory_value", sa.Text, nullable=False, server_default="0"),
        sa.Column("max_unpaired_inventory", sa.Text, nullable=False, server_default="0"),
        sa.Column("avg_unpaired_inventory", sa.Text, nullable=False, server_default="0"),
        sa.Column("bond_inventory_created", sa.Text, nullable=False, server_default="0"),
        sa.Column("bond_inventory_merged", sa.Text, nullable=False, server_default="0"),
        sa.Column("capital_turnover_ratio", sa.Text, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("simulation_lifecycle_summary")
    op.drop_index("ix_simulation_lifecycle_events_run", table_name="simulation_lifecycle_events")
    op.drop_table("simulation_lifecycle_events")
