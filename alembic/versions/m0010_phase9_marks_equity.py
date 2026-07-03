"""phase 9 marks and daily equity

Revision ID: m0010
Revises: m0009
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0010"
down_revision: Union[str, None] = "m0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_points",
        sa.Column("token_id", sa.Text, nullable=False),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("price", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("mark_age_s", sa.Integer, nullable=False),
        sa.Column("stale", sa.Integer, nullable=False),
        sa.Column("meta_json", sa.Text, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("token_id", "ts", "source"),
    )
    op.create_index("ix_price_points_token_ts", "price_points", ["token_id", "ts"])

    op.create_table(
        "daily_equity",
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("date", sa.Text, nullable=False),
        sa.Column("portfolio_value", sa.Text, nullable=False),
        sa.Column("realized_pnl_cum", sa.Text, nullable=False),
        sa.Column("unrealized_pnl", sa.Text, nullable=False),
        sa.Column("reward_income_cum", sa.Text, nullable=False),
        sa.Column("drawdown", sa.Text, nullable=False),
        sa.Column("stale_equity_share", sa.Text, nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("wallet", "date"),
    )
    op.create_index("ix_daily_equity_wallet_date", "daily_equity", ["wallet", "date"])


def downgrade() -> None:
    op.drop_index("ix_daily_equity_wallet_date", table_name="daily_equity")
    op.drop_table("daily_equity")
    op.drop_index("ix_price_points_token_ts", table_name="price_points")
    op.drop_table("price_points")
