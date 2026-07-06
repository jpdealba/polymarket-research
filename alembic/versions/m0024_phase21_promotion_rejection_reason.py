"""phase 21 promotion rejection reason

Revision ID: m0024
Revises: m0023
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

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


def downgrade() -> None:
    with op.batch_alter_table("rule_evaluations") as batch_op:
        batch_op.drop_column("promotion_rejection_reason")
