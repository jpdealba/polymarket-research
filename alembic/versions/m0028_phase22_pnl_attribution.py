"""phase 22 pnl attribution tables

Revision ID: m0028
Revises: m0027
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0028"
down_revision: Union[str, None] = "m0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "simulation_pnl_by_market",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("condition_id", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=True),
        sa.Column("event_id", sa.Text, nullable=True),
        sa.Column("fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fill_notional", sa.Text, nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("total_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("max_inventory", sa.Text, nullable=False, server_default="0"),
        sa.Column("turnover", sa.Text, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_simulation_pnl_by_market_run",
        "simulation_pnl_by_market",
        ["run_id", "total_pnl"],
    )

    op.create_table(
        "simulation_pnl_by_event",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("event_title", sa.Text, nullable=True),
        sa.Column("markets_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fill_notional", sa.Text, nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("total_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("max_event_exposure", sa.Text, nullable=False, server_default="0"),
        sa.Column("turnover", sa.Text, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_simulation_pnl_by_event_run",
        "simulation_pnl_by_event",
        ["run_id", "total_pnl"],
    )


def downgrade() -> None:
    op.drop_index("ix_simulation_pnl_by_event_run", table_name="simulation_pnl_by_event")
    op.drop_table("simulation_pnl_by_event")
    op.drop_index("ix_simulation_pnl_by_market_run", table_name="simulation_pnl_by_market")
    op.drop_table("simulation_pnl_by_market")
