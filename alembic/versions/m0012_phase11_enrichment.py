"""phase 11 maker/taker enrichment

Revision ID: m0012
Revises: m0011
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0012"
down_revision: Union[str, None] = "m0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fill_enrichment",
        sa.Column("event_id", sa.Integer, sa.ForeignKey("wallet_events.id"), nullable=False),
        # maker | taker — the role the enriched wallet played in this fill.
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("order_hash", sa.Text, nullable=False),
        # Decimal string (6-decimal on-chain fee normalized to units).
        sa.Column("fee", sa.Text, nullable=False),
        # The other side's address if available, else NULL.
        sa.Column("counterparty", sa.Text, nullable=True),
        # subgraph | rpc
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("enriched_at", sa.Text, nullable=False),
        # One enrichment row per ledger event — makes re-runs idempotent via
        # INSERT OR IGNORE / ON CONFLICT(event_id) DO NOTHING.
        sa.UniqueConstraint("event_id", name="uq_fill_enrichment_event_id"),
    )
    op.create_index(
        "ix_fill_enrichment_event_id", "fill_enrichment", ["event_id"]
    )

    op.create_table(
        "enrichment_watermarks",
        sa.Column("wallet", sa.Text, primary_key=True),
        sa.Column("subgraph_synced_to_ts", sa.Integer, nullable=True),
        sa.Column("rpc_synced_to_block", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("enrichment_watermarks")
    op.drop_index("ix_fill_enrichment_event_id", table_name="fill_enrichment")
    op.drop_table("fill_enrichment")
