"""phase 18 world cup forward microstructure watch

Revision ID: m0019
Revises: m0018
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0019"
down_revision: Union[str, None] = "m0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
    )

    op.create_table(
        "watchlist_tokens",
        sa.Column("watchlist_id", sa.Integer, sa.ForeignKey("watchlists.id"), nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("market_id", sa.Text, nullable=True),
        sa.Column("question", sa.Text, nullable=True),
        sa.Column("outcome_label", sa.Text, nullable=True),
        sa.Column("market_category", sa.Text, nullable=True),
        sa.Column("market_slug", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("first_seen_ts", sa.Integer, nullable=False),
        sa.Column("last_seen_ts", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("watchlist_id", "token_id", name="pk_watchlist_tokens"),
    )
    op.create_index(
        "ix_watchlist_tokens_active_priority",
        "watchlist_tokens",
        ["watchlist_id", "is_active", "priority"],
    )
    op.create_index("ix_watchlist_tokens_token_id", "watchlist_tokens", ["token_id"])

    op.create_table(
        "book_sample_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("watchlist_id", sa.Integer, sa.ForeignKey("watchlists.id"), nullable=False),
        sa.Column("started_at", sa.Integer, nullable=False),
        sa.Column("finished_at", sa.Integer, nullable=True),
        sa.Column("selector_wallet_latest_event_ts", sa.Integer, nullable=True),
        sa.Column("selector_wallet_latest_event_utc", sa.Text, nullable=True),
        sa.Column("tokens_selected", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_sampled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("books_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("books_empty", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_book_sample_runs_watchlist_started",
        "book_sample_runs",
        ["watchlist_id", "started_at"],
    )

    with op.batch_alter_table("book_snapshots") as batch_op:
        batch_op.add_column(sa.Column("sample_run_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("watchlist_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("selector_reason", sa.Text, nullable=True))
    op.create_index(
        "ix_book_snapshots_watchlist_ts",
        "book_snapshots",
        ["watchlist_id", "ts"],
    )
    op.create_index(
        "ix_book_snapshots_sample_run",
        "book_snapshots",
        ["sample_run_id"],
    )

    op.create_table(
        "maker_fill_context",
        sa.Column("event_id", sa.Integer, sa.ForeignKey("wallet_events.id"), primary_key=True),
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
        sa.Column("book_before_ts", sa.Integer, nullable=True),
        sa.Column("book_before_age_s", sa.Integer, nullable=True),
        sa.Column("best_bid_before", sa.Text, nullable=True),
        sa.Column("best_ask_before", sa.Text, nullable=True),
        sa.Column("spread_before", sa.Text, nullable=True),
        sa.Column("mid_before", sa.Text, nullable=True),
        sa.Column("depth_top_before_json", sa.Text, nullable=True),
        sa.Column("book_after_ts", sa.Integer, nullable=True),
        sa.Column("book_after_age_s", sa.Integer, nullable=True),
        sa.Column("best_bid_after", sa.Text, nullable=True),
        sa.Column("best_ask_after", sa.Text, nullable=True),
        sa.Column("spread_after", sa.Text, nullable=True),
        sa.Column("mid_after", sa.Text, nullable=True),
        sa.Column("depth_top_after_json", sa.Text, nullable=True),
        sa.Column("context_status", sa.Text, nullable=False),
        sa.Column("null_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_maker_fill_context_wallet_trade_ts",
        "maker_fill_context",
        ["wallet", "trade_ts"],
    )
    op.create_index(
        "ix_maker_fill_context_token_trade_ts",
        "maker_fill_context",
        ["token_id", "trade_ts"],
    )
    op.create_index(
        "ix_maker_fill_context_status",
        "maker_fill_context",
        ["context_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_maker_fill_context_status", table_name="maker_fill_context")
    op.drop_index("ix_maker_fill_context_token_trade_ts", table_name="maker_fill_context")
    op.drop_index("ix_maker_fill_context_wallet_trade_ts", table_name="maker_fill_context")
    op.drop_table("maker_fill_context")
    op.drop_index("ix_book_snapshots_sample_run", table_name="book_snapshots")
    op.drop_index("ix_book_snapshots_watchlist_ts", table_name="book_snapshots")
    with op.batch_alter_table("book_snapshots") as batch_op:
        batch_op.drop_column("selector_reason")
        batch_op.drop_column("watchlist_id")
        batch_op.drop_column("sample_run_id")
    op.drop_index("ix_book_sample_runs_watchlist_started", table_name="book_sample_runs")
    op.drop_table("book_sample_runs")
    op.drop_index("ix_watchlist_tokens_token_id", table_name="watchlist_tokens")
    op.drop_index("ix_watchlist_tokens_active_priority", table_name="watchlist_tokens")
    op.drop_table("watchlist_tokens")
    op.drop_table("watchlists")
