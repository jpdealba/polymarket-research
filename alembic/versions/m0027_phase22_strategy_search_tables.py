"""phase 22.2 strategy search tables

Revision ID: m0027
Revises: m0026
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0027"
down_revision: Union[str, None] = "m0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "simulation_strategy_search_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("strategy_family", sa.Text, nullable=False),
        sa.Column("seed", sa.Integer, nullable=False),
        sa.Column("max_combos", sa.Integer, nullable=False),
        sa.Column("total_combos", sa.Integer, nullable=False),
        sa.Column("evaluated_combos", sa.Integer, nullable=False, server_default="0"),
        sa.Column("selected_candidate_id", sa.Integer, nullable=True),
        sa.Column("run_ts", sa.Integer, nullable=False),
        sa.Column("elapsed_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
    )
    op.create_index(
        "ix_strategy_search_runs_wallet_rule",
        "simulation_strategy_search_runs",
        ["wallet", "rule_name", "run_ts"],
    )

    op.create_table(
        "simulation_strategy_candidates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "search_run_id",
            sa.Integer,
            sa.ForeignKey("simulation_strategy_search_runs.id"),
            nullable=False,
        ),
        sa.Column("candidate_index", sa.Integer, nullable=False),
        sa.Column("strategy_name", sa.Text, nullable=False),
        sa.Column("parameter_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("rank_index", sa.Integer, nullable=True),
        sa.Column("eligible", sa.Integer, nullable=False, server_default="0"),
        sa.Column("selected_for_test", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_strategy_candidates_run_rank",
        "simulation_strategy_candidates",
        ["search_run_id", "rank_index"],
    )

    op.create_table(
        "simulation_strategy_candidate_metrics",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id",
            sa.Integer,
            sa.ForeignKey("simulation_strategy_candidates.id"),
            nullable=False,
        ),
        sa.Column("split_name", sa.Text, nullable=False),
        sa.Column("candidate_signals_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("accepted_orders_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_orders_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_by_reason_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("simulated_fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fill_rate_on_candidates", sa.Text, nullable=True),
        sa.Column("net_pnl", sa.Text, nullable=False, server_default="0"),
        sa.Column("max_drawdown", sa.Text, nullable=False, server_default="0"),
        sa.Column("max_inventory", sa.Text, nullable=False, server_default="0"),
        sa.Column("capital_required", sa.Text, nullable=False, server_default="0"),
        sa.Column("turnover", sa.Text, nullable=False, server_default="0"),
        sa.Column("risk_breaches", sa.Integer, nullable=False, server_default="0"),
        sa.Column("risk_prevented_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ordering_violation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("conservative_pass", sa.Integer, nullable=False, server_default="0"),
        sa.Column("score", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_strategy_candidate_metrics_candidate_split",
        "simulation_strategy_candidate_metrics",
        ["candidate_id", "split_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_candidate_metrics_candidate_split",
        table_name="simulation_strategy_candidate_metrics",
    )
    op.drop_table("simulation_strategy_candidate_metrics")
    op.drop_index("ix_strategy_candidates_run_rank", table_name="simulation_strategy_candidates")
    op.drop_table("simulation_strategy_candidates")
    op.drop_index(
        "ix_strategy_search_runs_wallet_rule",
        table_name="simulation_strategy_search_runs",
    )
    op.drop_table("simulation_strategy_search_runs")
