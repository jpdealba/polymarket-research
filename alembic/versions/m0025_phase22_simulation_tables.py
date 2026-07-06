"""phase 22 simulation tables

Revision ID: m0025
Revises: m0024
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0025"
down_revision: Union[str, None] = "m0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("strategy_name", sa.Text, nullable=False),
        sa.Column("rule_version", sa.Integer, nullable=False),
        sa.Column("scenario", sa.Text, nullable=False),
        sa.Column("parameters_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("risk_limits_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("orders_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("simulated_fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fill_rate", sa.Text, nullable=True),
        sa.Column("simulated_pnl", sa.Text, nullable=True),
        sa.Column("net_pnl", sa.Text, nullable=True),
        sa.Column("max_drawdown", sa.Text, nullable=True),
        sa.Column("max_inventory", sa.Text, nullable=True),
        sa.Column("capital_required", sa.Text, nullable=True),
        sa.Column("turnover", sa.Text, nullable=True),
        sa.Column("skipped_orders_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_by_reason_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("risk_prevented_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("risk_breaches", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stale_context_excluded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("conservative_pass", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ordering_violation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("run_ts", sa.Integer, nullable=False),
        sa.Column("elapsed_ms", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_simulation_runs_wallet_rule",
        "simulation_runs",
        ["wallet", "rule_name", "rule_version", "scenario"],
    )
    op.create_index(
        "ix_simulation_runs_wallet_strategy",
        "simulation_runs",
        ["wallet", "strategy_name", "scenario"],
    )

    op.create_table(
        "simulation_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("event_id", sa.Integer, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("order_price", sa.Text, nullable=False),
        sa.Column("order_size", sa.Text, nullable=False),
        sa.Column("rule_fires", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rule_explanation", sa.Text, nullable=True),
        sa.Column("context_status", sa.Text, nullable=True),
        sa.Column("book_age_s", sa.Integer, nullable=True),
        sa.Column("stale_excluded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_ts", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_simulation_orders_run",
        "simulation_orders",
        ["run_id"],
    )

    op.create_table(
        "simulation_skipped_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("event_id", sa.Integer, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("strategy_name", sa.Text, nullable=False),
        sa.Column("base_rule", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=True),
        sa.Column("order_price", sa.Text, nullable=True),
        sa.Column("order_size", sa.Text, nullable=True),
        sa.Column("skipped_reason", sa.Text, nullable=False),
        sa.Column("context_status", sa.Text, nullable=True),
        sa.Column("book_age_s", sa.Integer, nullable=True),
        sa.Column("created_ts", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_simulation_skipped_orders_run",
        "simulation_skipped_orders",
        ["run_id"],
    )

    op.create_table(
        "simulation_fills",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("simulation_orders.id"), nullable=False),
        sa.Column("event_id", sa.Integer, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("fill_price", sa.Text, nullable=False),
        sa.Column("fill_size", sa.Text, nullable=False),
        sa.Column("fill_notional_usdc", sa.Text, nullable=False),
        sa.Column("estimated_fee", sa.Text, nullable=True),
        sa.Column("scenario", sa.Text, nullable=False),
        sa.Column("fill_reason", sa.Text, nullable=True),
        sa.Column("filled_ts", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_simulation_fills_run",
        "simulation_fills",
        ["run_id"],
    )

    op.create_table(
        "simulation_inventory",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("event_id", sa.Integer, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("qty_token", sa.Text, nullable=False, server_default="0"),
        sa.Column("qty_complement", sa.Text, nullable=False, server_default="0"),
        sa.Column("directional", sa.Text, nullable=False, server_default="0"),
        sa.Column("bond", sa.Text, nullable=False, server_default="0"),
        sa.Column("cost_basis", sa.Text, nullable=False, server_default="0"),
        sa.Column("mark_price", sa.Text, nullable=True),
        sa.Column("unrealized_pnl", sa.Text, nullable=True),
        sa.Column("event_exposure", sa.Text, nullable=True),
        sa.Column("snapshot_ts", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_simulation_inventory_run",
        "simulation_inventory",
        ["run_id"],
    )

    op.create_table(
        "simulation_pnl_daily",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("date_utc", sa.Text, nullable=False),
        sa.Column("realized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("total_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("cumulative_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("peak_portfolio", sa.Text, nullable=True),
        sa.Column("drawdown", sa.Text, nullable=True),
        sa.Column("fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("turnover", sa.Text, nullable=False, server_default="0"),
        sa.Column("risk_breaches", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_simulation_pnl_daily_run",
        "simulation_pnl_daily",
        ["run_id"],
    )

    op.create_table(
        "simulation_risk_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("simulation_runs.id"), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("limit_name", sa.Text, nullable=False),
        sa.Column("limit_value", sa.Text, nullable=False),
        sa.Column("actual_value", sa.Text, nullable=False),
        sa.Column("token_id", sa.Text, nullable=True),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("timestamp", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_simulation_risk_events_run",
        "simulation_risk_events",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_table("simulation_risk_events")
    op.drop_table("simulation_pnl_daily")
    op.drop_table("simulation_inventory")
    op.drop_table("simulation_fills")
    op.drop_table("simulation_skipped_orders")
    op.drop_table("simulation_orders")
    op.drop_table("simulation_runs")
