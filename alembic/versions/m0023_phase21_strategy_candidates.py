"""phase 21 interpretable rule reconstruction — strategy candidates + rule evaluations

Revision ID: m0023
Revises: m0022
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0023"
down_revision: Union[str, None] = "m0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_candidates",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("rule_version", sa.Integer, nullable=False),
        # Tuned parameters as JSON.
        sa.Column("parameters_json", sa.Text, nullable=False),
        # Feature columns used by this rule (JSON array).
        sa.Column("features_used_json", sa.Text, nullable=False),
        # Whether the rule passed out-of-sample validation.
        sa.Column("promoted", sa.Integer, nullable=False, server_default="0"),
        # Aggregate metrics.
        sa.Column("explained_fills_pct", sa.Text, nullable=False),
        sa.Column("expected_pnl_or_markout", sa.Text, nullable=True),
        sa.Column("inventory_impact", sa.Text, nullable=True),
        sa.Column("risk_requirements", sa.Text, nullable=False),
        sa.Column("blind_spots", sa.Text, nullable=False),
        sa.Column("fitted_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "rule_name", "rule_version"),
    )
    op.create_index(
        "ix_strategy_candidates_wallet", "strategy_candidates", ["wallet"]
    )
    op.create_index(
        "ix_strategy_candidates_promoted",
        "strategy_candidates",
        ["wallet", "promoted"],
    )

    op.create_table(
        "rule_evaluations",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("rule_version", sa.Integer, nullable=False),
        sa.Column("window", sa.Text, nullable=False),
        # Per-window metrics.
        sa.Column("total_fills", sa.Integer, nullable=False),
        sa.Column("explained_fills", sa.Integer, nullable=False),
        sa.Column("fill_explained_rate", sa.Text, nullable=False),
        sa.Column("precision", sa.Text, nullable=False),
        sa.Column("coverage", sa.Text, nullable=False),
        sa.Column("avg_markout_5m", sa.Text, nullable=True),
        sa.Column("avg_markout_1h", sa.Text, nullable=True),
        sa.Column("avg_pnl_episode", sa.Text, nullable=True),
        sa.Column("avg_bond_delta", sa.Text, nullable=True),
        sa.Column("avg_exposure_delta", sa.Text, nullable=True),
        sa.Column("max_inventory_required", sa.Text, nullable=True),
        sa.Column("out_of_sample_edge_bps", sa.Text, nullable=True),
        sa.Column("out_of_sample_pnl", sa.Text, nullable=True),
        sa.Column("evaluated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "rule_name", "rule_version", "window"),
    )
    op.create_index(
        "ix_rule_evaluations_wallet",
        "rule_evaluations",
        ["wallet", "rule_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_rule_evaluations_wallet", table_name="rule_evaluations")
    op.drop_table("rule_evaluations")
    op.drop_index("ix_strategy_candidates_promoted", table_name="strategy_candidates")
    op.drop_index("ix_strategy_candidates_wallet", table_name="strategy_candidates")
    op.drop_table("strategy_candidates")
