"""phase 14 strategy detector labels

Revision ID: m0017
Revises: m0016
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0017"
down_revision: Union[str, None] = "m0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_labels",
        sa.Column("wallet", sa.Text, nullable=False),
        # "all" or "category:<Label>" — the fingerprint scope this label reads.
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("detector_name", sa.Text, nullable=False),
        sa.Column("detector_version", sa.Integer, nullable=False),
        # The hypothesis name (== detector_name). Never a boolean verdict.
        sa.Column("label", sa.Text, nullable=False),
        # Score in [0, 1]: weighted mean of the detector's per-feature sub-scores
        # over the features actually available (NULL features are excluded, not
        # treated as 0). Decimal string.
        sa.Column("score", sa.Text, nullable=False),
        # Share of the detector's total feature weight that was available
        # (missing/NULL features lower this). Decimal string. Low confidence =
        # the score leans on few inputs; read it alongside score.
        sa.Column("confidence", sa.Text, nullable=False),
        # Machine-readable evidence: {"features": {feature: {value, weight,
        # sub_score, null_reason}}, "confidence": ..., "missing_features": [...],
        # "score_formula": ...}. Every input feature+value is recorded, NULLs
        # included — never prose.
        sa.Column("evidence_json", sa.Text, nullable=False),
        # What the detector structurally cannot see (free text).
        sa.Column("blind_spots", sa.Text, nullable=False),
        sa.Column("computed_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "wallet", "scope", "detector_name", "detector_version", "computed_at"
        ),
    )
    op.create_index(
        "ix_strategy_labels_wallet", "strategy_labels", ["wallet", "detector_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_labels_wallet", table_name="strategy_labels")
    op.drop_table("strategy_labels")
