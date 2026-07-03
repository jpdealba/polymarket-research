"""wallets, watchlist, sync_state, raw_fetches

Revision ID: m0002
Revises: m0001
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0002"
down_revision: Union[str, None] = "m0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("address", sa.Text, primary_key=True),
        sa.Column("first_seen_at", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=True),
    )
    op.create_table(
        "watchlist",
        sa.Column("wallet", sa.Text, sa.ForeignKey("wallets.address"), primary_key=True),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("added_at", sa.Text, nullable=False),
        sa.Column("removed_at", sa.Text, nullable=True),
    )
    op.create_table(
        "sync_state",
        sa.Column("wallet", sa.Text, sa.ForeignKey("wallets.address"), primary_key=True),
        sa.Column("backfill_complete", sa.Integer, nullable=False, server_default="0"),
        sa.Column("backfill_cursor_ts", sa.Integer, nullable=True),
        sa.Column("last_incremental_ts", sa.Integer, nullable=True),
        sa.Column("last_success_at", sa.Text, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
    )
    op.create_table(
        "raw_fetches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("params_json", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.Text, nullable=False),
        sa.Column("http_status", sa.Integer, nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_raw_fetches_dedupe",
        "raw_fetches",
        ["source", "endpoint", "params_json", "content_hash"],
    )
    op.create_index("ix_raw_fetches_source_endpoint", "raw_fetches", ["source", "endpoint"])


def downgrade() -> None:
    op.drop_index("ix_raw_fetches_source_endpoint", table_name="raw_fetches")
    op.drop_index("ix_raw_fetches_dedupe", table_name="raw_fetches")
    op.drop_table("raw_fetches")
    op.drop_table("sync_state")
    op.drop_table("watchlist")
    op.drop_table("wallets")
