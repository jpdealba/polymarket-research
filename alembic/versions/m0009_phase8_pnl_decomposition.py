"""phase 8 pnl decomposition

Revision ID: m0009
Revises: m0008
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0009"
down_revision: Union[str, None] = "m0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pnl_decomposition",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("period", sa.Text, nullable=False, server_default="all"),
        sa.Column("directional_pnl", sa.Text, nullable=False),
        sa.Column("bond_merge_pnl", sa.Text, nullable=False),
        sa.Column("reward_income", sa.Text, nullable=False),
        sa.Column("redemption_pnl", sa.Text, nullable=False),
        sa.Column("fees", sa.Text, nullable=False),
        sa.Column("computed_at", sa.Text, nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_pnl_decomposition_wallet_scope_period",
        "pnl_decomposition",
        ["wallet", "scope", "period"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_pnl_decomposition_wallet_scope_period", table_name="pnl_decomposition")
    op.drop_table("pnl_decomposition")
