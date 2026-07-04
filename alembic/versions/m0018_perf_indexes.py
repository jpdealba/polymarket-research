"""performance indexes

Revision ID: m0018
Revises: m0017
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m0018"
down_revision: Union[str, None] = "m0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ingest scan: WHERE source='dataapi' AND endpoint='activity' AND ingested_at IS NULL
    op.create_index(
        "ix_raw_fetches_source_endpoint_ingested",
        "raw_fetches",
        ["source", "endpoint", "ingested_at"],
    )
    # Fee attribution & event-type-filtered queries: WHERE wallet=:w AND event_type=:t ORDER BY ts
    op.create_index(
        "ix_wallet_events_wallet_event_type_ts",
        "wallet_events",
        ["wallet", "event_type", "ts"],
    )
    # Last-trade-mark fallback: WHERE token_id=:t AND event_type='TRADE' ORDER BY ts DESC
    op.create_index(
        "ix_wallet_events_token_event_type_ts",
        "wallet_events",
        ["token_id", "event_type", "ts"],
    )
    # Scheduler: WHERE closed=0 AND end_date IS NOT NULL AND end_date<=datetime('now')
    op.create_index(
        "ix_markets_closed_end_date",
        "markets",
        ["closed", "end_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_markets_closed_end_date", table_name="markets")
    op.drop_index("ix_wallet_events_token_event_type_ts", table_name="wallet_events")
    op.drop_index("ix_wallet_events_wallet_event_type_ts", table_name="wallet_events")
    op.drop_index("ix_raw_fetches_source_endpoint_ingested", table_name="raw_fetches")
