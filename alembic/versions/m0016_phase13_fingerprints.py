"""phase 13 behavioral fingerprints

Revision ID: m0016
Revises: m0015
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0016"
down_revision: Union[str, None] = "m0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fingerprints",
        sa.Column("wallet", sa.Text, nullable=False),
        # "all" or "category:<Label>" (e.g. "category:Sports", "category:unknown").
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("feature", sa.Text, nullable=False),
        sa.Column("family", sa.Text, nullable=False),
        # Decimal string (scalar) or JSON object (distribution); NULL when the
        # feature is uncomputable for this wallet/scope/window — the reason is
        # then carried in null_reason. Never silently 0.
        sa.Column("value", sa.Text, nullable=True),
        # "scalar" | "json" — how to parse `value`. NULL when value is NULL.
        sa.Column("value_type", sa.Text, nullable=True),
        # Human-readable reason `value` is NULL; NULL when value is present.
        sa.Column("null_reason", sa.Text, nullable=True),
        # "all" (full history) or "90d" (trailing 90 days of the wallet's own
        # activity timeline, relative to its latest event — reproducible).
        sa.Column("window", sa.Text, nullable=False),
        sa.Column("computed_at", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint(
            "wallet", "scope", "feature", "window", "version"
        ),
    )
    op.create_index(
        "ix_fingerprints_wallet_window", "fingerprints", ["wallet", "window"]
    )


def downgrade() -> None:
    op.drop_index("ix_fingerprints_wallet_window", table_name="fingerprints")
    op.drop_table("fingerprints")
