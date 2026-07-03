"""episodes projection

Revision ID: m0008
Revises: m0007
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0008"
down_revision: Union[str, None] = "m0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("open_ts", sa.Integer, nullable=False),
        sa.Column("close_ts", sa.Integer, nullable=True),
        sa.Column("close_reason", sa.Text, nullable=False),
        sa.Column("peak_qty", sa.Text, nullable=False),
        sa.Column("num_adds", sa.Integer, nullable=False),
        sa.Column("num_partial_exits", sa.Integer, nullable=False),
        sa.Column("wac_entry", sa.Text, nullable=False),
        sa.Column("realized_pnl", sa.Text, nullable=False),
        sa.Column("reward_income", sa.Text, nullable=False),
        sa.Column("fees_paid", sa.Text, nullable=True),
        # JSON array of wallet_events.id values consumed by this token episode.
        sa.Column("events_consumed", sa.Text, nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False),
    )
    op.create_index("ix_episodes_wallet_open_ts", "episodes", ["wallet", "open_ts"])
    op.create_index("ix_episodes_wallet_token", "episodes", ["wallet", "token_id"])


def downgrade() -> None:
    op.drop_index("ix_episodes_wallet_token", table_name="episodes")
    op.drop_index("ix_episodes_wallet_open_ts", table_name="episodes")
    op.drop_table("episodes")
