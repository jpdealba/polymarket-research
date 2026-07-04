"""world cup tracked wallets

Revision ID: m0020
Revises: m0019
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "m0020"
down_revision: Union[str, None] = "m0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worldcup_tracked_wallets",
        sa.Column("wallet", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("selected_at", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_worldcup_tracked_wallets_active_priority",
        "worldcup_tracked_wallets",
        ["is_active", "priority"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worldcup_tracked_wallets_active_priority",
        table_name="worldcup_tracked_wallets",
    )
    op.drop_table("worldcup_tracked_wallets")
