"""daily equity marked drawdown basis

Revision ID: m0015
Revises: m0014
Create Date: 2026-07-04

"""
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "m0015"
down_revision: Union[str, None] = "m0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "daily_equity",
        sa.Column("marked_pnl", sa.Text, nullable=False, server_default="0"),
    )
    op.add_column(
        "daily_equity",
        sa.Column("drawdown_basis", sa.Text, nullable=False, server_default="marked_pnl"),
    )
    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT wallet, date, realized_pnl_cum, unrealized_pnl, reward_income_cum "
            "FROM daily_equity ORDER BY wallet, date"
        )
    ).fetchall()
    peak_by_wallet: dict[str, Decimal] = {}
    for row in rows:
        marked_pnl = (
            Decimal(str(row.realized_pnl_cum or 0))
            + Decimal(str(row.unrealized_pnl or 0))
            + Decimal(str(row.reward_income_cum or 0))
        )
        peak = peak_by_wallet.get(row.wallet)
        if peak is None or marked_pnl > peak:
            peak = marked_pnl
            peak_by_wallet[row.wallet] = peak
        drawdown = peak - marked_pnl
        bind.execute(
            text(
                "UPDATE daily_equity SET marked_pnl = :marked_pnl, "
                "drawdown = :drawdown, drawdown_basis = 'marked_pnl', "
                "projection_version = 2 WHERE wallet = :wallet AND date = :date"
            ),
            {
                "marked_pnl": str(marked_pnl),
                "drawdown": str(drawdown),
                "wallet": row.wallet,
                "date": row.date,
            },
        )


def downgrade() -> None:
    op.drop_column("daily_equity", "drawdown_basis")
    op.drop_column("daily_equity", "marked_pnl")
