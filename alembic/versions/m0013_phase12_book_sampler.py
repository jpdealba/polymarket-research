"""phase 12 book sampler

Revision ID: m0013
Revises: m0012
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0013"
down_revision: Union[str, None] = "m0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "book_snapshots",
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("best_bid", sa.Text, nullable=True),
        sa.Column("best_ask", sa.Text, nullable=True),
        sa.Column("spread", sa.Text, nullable=True),
        sa.Column("mid", sa.Text, nullable=True),
        # Top-10 depth as JSON string: {"bids": [{"price","size"},...], "asks": [...]}
        sa.Column("depth_top_json", sa.Text, nullable=True),
        # FK to raw_fetches — nullable after raw pruning removes the file.
        sa.Column("raw_ref", sa.Integer, sa.ForeignKey("raw_fetches.id"), nullable=True),
        sa.PrimaryKeyConstraint("token_id", "ts", name="pk_book_snapshots"),
    )
    op.create_index(
        "ix_book_snapshots_token_ts", "book_snapshots", ["token_id", "ts"]
    )
    op.create_index(
        "ix_book_snapshots_ts", "book_snapshots", ["ts"]
    )

    # Tiny key-value table for sampler rotation state.
    op.create_table(
        "_book_sampler_state",
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("_book_sampler_state")
    op.drop_index("ix_book_snapshots_ts", table_name="book_snapshots")
    op.drop_index("ix_book_snapshots_token_ts", table_name="book_snapshots")
    op.drop_table("book_snapshots")
