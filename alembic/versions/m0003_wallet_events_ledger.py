"""wallet_events ledger + raw_fetches.ingested_at

Revision ID: m0003
Revises: m0002
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0003"
down_revision: Union[str, None] = "m0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("tx_hash", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("token_id", sa.Text, nullable=True),
        sa.Column("side", sa.Text, nullable=True),
        # Decimal strings, not REAL: SQLite has no fixed-point type, and
        # these are financial quantities Phase 4+ accumulates in Decimal.
        sa.Column("delta_shares", sa.Text, nullable=False),
        sa.Column("delta_usdc", sa.Text, nullable=False),
        sa.Column("price", sa.Text, nullable=False),
        sa.Column("usdc_size", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("is_derived", sa.Integer, nullable=False, server_default="0"),
        sa.Column("raw_ref", sa.Integer, sa.ForeignKey("raw_fetches.id"), nullable=False),
        sa.Column("dedupe_key", sa.Text, nullable=False, unique=True),
        sa.Column("ingested_at", sa.Text, nullable=False),
    )
    op.create_index("ix_wallet_events_wallet_ts", "wallet_events", ["wallet", "ts"])
    op.create_index("ix_wallet_events_token_id", "wallet_events", ["token_id"])
    op.create_index("ix_wallet_events_tx_hash", "wallet_events", ["tx_hash"])
    op.create_index("ix_wallet_events_condition_id", "wallet_events", ["condition_id"])

    with op.batch_alter_table("raw_fetches") as batch_op:
        batch_op.add_column(sa.Column("ingested_at", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("raw_fetches") as batch_op:
        batch_op.drop_column("ingested_at")
    op.drop_index("ix_wallet_events_condition_id", table_name="wallet_events")
    op.drop_index("ix_wallet_events_tx_hash", table_name="wallet_events")
    op.drop_index("ix_wallet_events_token_id", table_name="wallet_events")
    op.drop_index("ix_wallet_events_wallet_ts", table_name="wallet_events")
    op.drop_table("wallet_events")
