"""make raw_fetch dedupe index unique

Revision ID: m0003b
Revises: m0003
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m0003b"
down_revision: Union[str, None] = "m0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_raw_fetches_dedupe", table_name="raw_fetches")
    op.create_index(
        "ux_raw_fetches_dedupe",
        "raw_fetches",
        ["source", "endpoint", "params_json", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_raw_fetches_dedupe", table_name="raw_fetches")
    op.create_index(
        "ix_raw_fetches_dedupe",
        "raw_fetches",
        ["source", "endpoint", "params_json", "content_hash"],
    )
