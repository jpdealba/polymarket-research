"""phase 21 promotion rejection reason

Revision ID: m0024
Revises: m0023
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "m0024"
down_revision: Union[str, None] = "m0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("rule_evaluations") as batch_op:
        batch_op.add_column(
            sa.Column("promotion_rejection_reason", sa.Text, nullable=True)
        )

    conn = op.get_bind()
    degenerate_event_timing = (
        "rule_name = 'event_timing' "
        "AND parameters_json LIKE '%\"allowed_hours_utc\": []%' "
        "AND parameters_json LIKE '%\"max_time_to_event_start_s\": null%' "
        "AND parameters_json LIKE '%\"min_time_to_event_start_s\": null%'"
    )
    conn.execute(
        text(
            "UPDATE strategy_candidates SET promoted = 0 "
            f"WHERE {degenerate_event_timing}"
        )
    )
    conn.execute(
        text(
            "UPDATE rule_evaluations "
            "SET promotion_rejection_reason = 'no_active_predicate' "
            "WHERE rule_name = 'event_timing' "
            "AND EXISTS ("
            "  SELECT 1 FROM strategy_candidates sc "
            "  WHERE sc.wallet = rule_evaluations.wallet "
            "  AND sc.rule_name = rule_evaluations.rule_name "
            "  AND sc.rule_version = rule_evaluations.rule_version "
            "  AND sc.parameters_json LIKE '%\"allowed_hours_utc\": []%' "
            "  AND sc.parameters_json LIKE '%\"max_time_to_event_start_s\": null%' "
            "  AND sc.parameters_json LIKE '%\"min_time_to_event_start_s\": null%'"
            ")"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("rule_evaluations") as batch_op:
        batch_op.drop_column("promotion_rejection_reason")
