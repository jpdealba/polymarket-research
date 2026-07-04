"""phase 11 fee integration

Revision ID: m0014
Revises: m0013
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0014"
down_revision: Union[str, None] = "m0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("fill_enrichment") as batch_op:
        batch_op.alter_column("fee", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "fee_estimates",
        sa.Column("fee_source", sa.Text, nullable=False, server_default="estimated_schedule"),
    )


def downgrade() -> None:
    op.drop_column("fee_estimates", "fee_source")
    with op.batch_alter_table("fill_enrichment") as batch_op:
        batch_op.alter_column("fee", existing_type=sa.Text(), nullable=False)
