"""Fee attribution estimates

Revision ID: m0005
Revises: m0004
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0005"
down_revision: Union[str, None] = "m0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fee_schedules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("effective_from_ts", sa.Integer, nullable=False),
        sa.Column("effective_to_ts", sa.Integer, nullable=True),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("params_json", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "category",
            "effective_from_ts",
            "rule_name",
            name="uq_fee_schedules_category_effective_rule",
        ),
    )
    op.create_table(
        "fee_estimates",
        sa.Column("event_id", sa.Integer, sa.ForeignKey("wallet_events.id"), primary_key=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("condition_id", sa.Text, nullable=True),
        sa.Column("token_id", sa.Text, nullable=True),
        sa.Column("category", sa.Text, nullable=True),
        sa.Column("ts", sa.Integer, nullable=False),
        sa.Column("estimated_fee", sa.Text, nullable=False),
        sa.Column("worst_case_fee", sa.Text, nullable=False),
        sa.Column("actual_fee", sa.Text, nullable=True),
        sa.Column("fee_currency", sa.Text, nullable=False),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False),
        sa.Column("computed_at", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_fee_schedules_category_effective",
        "fee_schedules",
        ["category", "effective_from_ts"],
    )
    op.create_index("ix_fee_estimates_wallet_ts", "fee_estimates", ["wallet", "ts"])
    op.create_index("ix_fee_estimates_category", "fee_estimates", ["category"])


def downgrade() -> None:
    op.drop_index("ix_fee_estimates_category", table_name="fee_estimates")
    op.drop_index("ix_fee_estimates_wallet_ts", table_name="fee_estimates")
    op.drop_index("ix_fee_schedules_category_effective", table_name="fee_schedules")
    op.drop_table("fee_estimates")
    op.drop_table("fee_schedules")
