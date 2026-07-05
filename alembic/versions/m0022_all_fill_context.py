"""all fill context and explicit fill size fields

Revision ID: m0022
Revises: m0021
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0022"
down_revision: Union[str, None] = "m0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "all_fill_context",
        sa.Column("event_id", sa.Integer, sa.ForeignKey("wallet_events.id"), primary_key=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("trade_ts", sa.Integer, nullable=False),
        sa.Column("trade_utc", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=True),
        sa.Column("fill_price", sa.Text, nullable=True),
        # Legacy name retained for compatibility: this is notional USDC.
        sa.Column("fill_size", sa.Text, nullable=True),
        sa.Column("fill_shares", sa.Text, nullable=True),
        sa.Column("fill_notional_usdc", sa.Text, nullable=True),
        sa.Column("delta_usdc", sa.Text, nullable=True),
        # NULL means maker/taker enrichment has not landed yet.
        sa.Column("role", sa.Text, nullable=True),
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
        "ix_all_fill_context_wallet_trade_ts",
        "all_fill_context",
        ["wallet", "trade_ts"],
    )
    op.create_index(
        "ix_all_fill_context_token_trade_ts",
        "all_fill_context",
        ["token_id", "trade_ts"],
    )
    op.create_index("ix_all_fill_context_status", "all_fill_context", ["context_status"])
    op.create_index("ix_all_fill_context_role", "all_fill_context", ["role"])

    with op.batch_alter_table("maker_fill_context") as batch_op:
        batch_op.add_column(sa.Column("fill_shares", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("fill_notional_usdc", sa.Text, nullable=True))

    with op.batch_alter_table("microstructure_lifecycle_dataset") as batch_op:
        batch_op.add_column(sa.Column("fill_shares", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("fill_notional_usdc", sa.Text, nullable=True))
        batch_op.add_column(
            sa.Column("context_source", sa.Text, nullable=False, server_default="maker_only")
        )


def downgrade() -> None:
    with op.batch_alter_table("microstructure_lifecycle_dataset") as batch_op:
        batch_op.drop_column("context_source")
        batch_op.drop_column("fill_notional_usdc")
        batch_op.drop_column("fill_shares")

    with op.batch_alter_table("maker_fill_context") as batch_op:
        batch_op.drop_column("fill_notional_usdc")
        batch_op.drop_column("fill_shares")

    op.drop_index("ix_all_fill_context_role", table_name="all_fill_context")
    op.drop_index("ix_all_fill_context_status", table_name="all_fill_context")
    op.drop_index("ix_all_fill_context_token_trade_ts", table_name="all_fill_context")
    op.drop_index("ix_all_fill_context_wallet_trade_ts", table_name="all_fill_context")
    op.drop_table("all_fill_context")
