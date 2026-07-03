"""reconciliation facts and wallet trust

Revision ID: m0007
Revises: m0006
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0007"
down_revision: Union[str, None] = "m0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_facts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("check_type", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        # Decimal strings, matching holdings/wallet_events accounting fields.
        sa.Column("expected", sa.Text, nullable=False),
        sa.Column("computed", sa.Text, nullable=False),
        sa.Column("abs_diff", sa.Text, nullable=False),
        sa.Column("pct_diff", sa.Text, nullable=False),
        sa.Column("tolerance", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("reason_code", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
    )
    op.create_index(
        "ix_reconciliation_facts_wallet_ts",
        "reconciliation_facts",
        ["wallet", "ts"],
    )
    op.create_index(
        "ix_reconciliation_facts_status",
        "reconciliation_facts",
        ["status"],
    )
    op.create_index(
        "ix_reconciliation_facts_reason_code",
        "reconciliation_facts",
        ["reason_code"],
    )
    op.create_table(
        "wallet_trust",
        sa.Column("wallet", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("since_ts", sa.Integer, nullable=False),
        sa.Column("updated_ts", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("last_reconciliation_ts", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("wallet_trust")
    op.drop_index("ix_reconciliation_facts_reason_code", table_name="reconciliation_facts")
    op.drop_index("ix_reconciliation_facts_status", table_name="reconciliation_facts")
    op.drop_index("ix_reconciliation_facts_wallet_ts", table_name="reconciliation_facts")
    op.drop_table("reconciliation_facts")
