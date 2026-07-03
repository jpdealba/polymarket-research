"""Gamma market metadata dimensions

Revision ID: m0004
Revises: m0003b
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0004"
down_revision: Union[str, None] = "m0003b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pm_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("slug", sa.Text, nullable=True),
        sa.Column("neg_risk", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tags_json", sa.Text, nullable=False, server_default="[]"),
    )
    op.create_table(
        "markets",
        sa.Column("condition_id", sa.Text, primary_key=True),
        sa.Column("question", sa.Text, nullable=True),
        sa.Column("slug", sa.Text, nullable=True),
        sa.Column("category", sa.Text, nullable=True),
        sa.Column("event_id", sa.Text, sa.ForeignKey("pm_events.event_id"), nullable=True),
        sa.Column("neg_risk", sa.Integer, nullable=False, server_default="0"),
        sa.Column("outcomes_json", sa.Text, nullable=False),
        sa.Column("clob_token_ids_json", sa.Text, nullable=False),
        sa.Column("start_date", sa.Text, nullable=True),
        sa.Column("end_date", sa.Text, nullable=True),
        sa.Column("closed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("resolution_prices_json", sa.Text, nullable=True),
        sa.Column("closed_time", sa.Text, nullable=True),
        sa.Column("structure_type", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_table(
        "tokens",
        sa.Column("token_id", sa.Text, primary_key=True),
        sa.Column("condition_id", sa.Text, sa.ForeignKey("markets.condition_id"), nullable=False),
        sa.Column("outcome_index", sa.Integer, nullable=False),
        sa.Column("outcome_label", sa.Text, nullable=True),
    )
    op.create_index("ix_markets_event_id", "markets", ["event_id"])
    op.create_index("ix_markets_structure_type", "markets", ["structure_type"])
    op.create_index("ix_tokens_condition_id", "tokens", ["condition_id"])


def downgrade() -> None:
    op.drop_index("ix_tokens_condition_id", table_name="tokens")
    op.drop_index("ix_markets_structure_type", table_name="markets")
    op.drop_index("ix_markets_event_id", table_name="markets")
    op.drop_table("tokens")
    op.drop_table("markets")
    op.drop_table("pm_events")

