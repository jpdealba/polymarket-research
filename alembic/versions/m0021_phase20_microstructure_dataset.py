"""phase 20 microstructure + lifecycle dataset

Revision ID: m0021
Revises: m0020
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0021"
down_revision: Union[str, None] = "m0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "microstructure_lifecycle_dataset",
        sa.Column("event_id", sa.Integer, sa.ForeignKey("wallet_events.id"), primary_key=True),
        # identity / copy-through from maker_fill_context
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("trade_ts", sa.Integer, nullable=False),
        sa.Column("trade_utc", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=True),
        sa.Column("fill_price", sa.Text, nullable=True),
        sa.Column("fill_size", sa.Text, nullable=True),
        sa.Column("delta_usdc", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("context_status", sa.Text, nullable=False),
        sa.Column("book_before_age_s", sa.Integer, nullable=True),
        sa.Column("book_after_age_s", sa.Integer, nullable=True),
        # book-before derived
        sa.Column("best_bid_before", sa.Text, nullable=True),
        sa.Column("best_ask_before", sa.Text, nullable=True),
        sa.Column("mid_before", sa.Text, nullable=True),
        sa.Column("spread_before", sa.Text, nullable=True),
        sa.Column("spread_bps", sa.Text, nullable=True),
        sa.Column("bid_depth_top1", sa.Text, nullable=True),
        sa.Column("ask_depth_top1", sa.Text, nullable=True),
        sa.Column("bid_depth_top5", sa.Text, nullable=True),
        sa.Column("ask_depth_top5", sa.Text, nullable=True),
        sa.Column("book_imbalance_top1", sa.Text, nullable=True),
        sa.Column("book_imbalance_top5", sa.Text, nullable=True),
        sa.Column("distance_fill_to_mid", sa.Text, nullable=True),
        sa.Column("distance_fill_to_bid", sa.Text, nullable=True),
        sa.Column("distance_fill_to_ask", sa.Text, nullable=True),
        sa.Column("fill_inside_spread", sa.Integer, nullable=True),
        sa.Column("fill_at_best_bid", sa.Integer, nullable=True),
        sa.Column("fill_at_best_ask", sa.Integer, nullable=True),
        # trade / market context
        sa.Column("trade_hour_utc", sa.Integer, nullable=True),
        sa.Column("market_category", sa.Text, nullable=True),
        sa.Column("time_to_event_start_s", sa.Integer, nullable=True),
        sa.Column("wallet_label", sa.Text, nullable=True),
        # inventory before/after
        sa.Column("qty_token_before", sa.Text, nullable=True),
        sa.Column("qty_complement_before", sa.Text, nullable=True),
        sa.Column("directional_before", sa.Text, nullable=True),
        sa.Column("bond_before", sa.Text, nullable=True),
        sa.Column("bond_ratio_before", sa.Text, nullable=True),
        sa.Column("qty_token_after", sa.Text, nullable=True),
        sa.Column("qty_complement_after", sa.Text, nullable=True),
        sa.Column("directional_after", sa.Text, nullable=True),
        sa.Column("bond_after", sa.Text, nullable=True),
        sa.Column("bond_ratio_after", sa.Text, nullable=True),
        sa.Column("bond_delta", sa.Text, nullable=True),
        sa.Column("directional_delta", sa.Text, nullable=True),
        # event-level negRisk exposure
        sa.Column("event_exposure_before", sa.Text, nullable=True),
        sa.Column("event_exposure_after", sa.Text, nullable=True),
        sa.Column("event_exposure_delta", sa.Text, nullable=True),
        # lifecycle / close path
        sa.Column("close_path", sa.Text, nullable=False),
        sa.Column("close_ts", sa.Integer, nullable=True),
        sa.Column("hold_seconds", sa.Integer, nullable=True),
        sa.Column("realized_pnl_wac", sa.Text, nullable=True),
        sa.Column("realized_pnl_per_share", sa.Text, nullable=True),
        sa.Column("realized_pnl_bps_on_cost", sa.Text, nullable=True),
        sa.Column("remaining_open_qty_after_24h", sa.Text, nullable=True),
        sa.Column("is_open_after_24h", sa.Integer, nullable=True),
        sa.Column("closed_by_merge", sa.Integer, nullable=False, server_default="0"),
        sa.Column("closed_by_redeem", sa.Integer, nullable=False, server_default="0"),
        sa.Column("closed_by_sell", sa.Integer, nullable=False, server_default="0"),
        sa.Column("closed_by_resolution", sa.Integer, nullable=False, server_default="0"),
        sa.Column("closed_by_unresolved_open", sa.Integer, nullable=False, server_default="0"),
        # labels
        sa.Column("markout_5m", sa.Text, nullable=True),
        sa.Column("markout_15m", sa.Text, nullable=True),
        sa.Column("markout_1h", sa.Text, nullable=True),
        sa.Column("markout_24h", sa.Text, nullable=True),
        sa.Column("pnl_episode", sa.Text, nullable=True),
        sa.Column("pnl_at_resolution", sa.Text, nullable=True),
        # bookkeeping
        sa.Column("null_reasons_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("dataset_version", sa.Integer, nullable=False),
        sa.Column("watchlist", sa.Text, nullable=False),
        sa.Column("built_at", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_microstructure_dataset_wallet_ts",
        "microstructure_lifecycle_dataset",
        ["wallet", "trade_ts"],
    )
    op.create_index(
        "ix_microstructure_dataset_token_ts",
        "microstructure_lifecycle_dataset",
        ["token_id", "trade_ts"],
    )
    op.create_index(
        "ix_microstructure_dataset_close_path",
        "microstructure_lifecycle_dataset",
        ["close_path"],
    )


def downgrade() -> None:
    op.drop_index("ix_microstructure_dataset_close_path", table_name="microstructure_lifecycle_dataset")
    op.drop_index("ix_microstructure_dataset_token_ts", table_name="microstructure_lifecycle_dataset")
    op.drop_index("ix_microstructure_dataset_wallet_ts", table_name="microstructure_lifecycle_dataset")
    op.drop_table("microstructure_lifecycle_dataset")
