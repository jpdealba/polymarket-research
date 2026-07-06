"""phase 22.1 strategy variant columns

Revision ID: m0026
Revises: m0025
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

# revision identifiers, used by Alembic.
revision: str = "m0026"
down_revision: Union[str, None] = "m0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "simulation_runs" not in tables:
        return

    indexes = {idx["name"] for idx in inspector.get_indexes("simulation_runs")}
    if "ix_simulation_runs_wallet_strategy" in indexes:
        op.drop_index(
            "ix_simulation_runs_wallet_strategy",
            table_name="simulation_runs",
        )

    columns = {col["name"] for col in inspector.get_columns("simulation_runs")}
    with op.batch_alter_table("simulation_runs") as batch_op:
        if "strategy_name" not in columns:
            batch_op.add_column(
                sa.Column("strategy_name", sa.Text, nullable=False, server_default="")
            )
        if "skipped_orders_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "skipped_orders_count",
                    sa.Integer,
                    nullable=False,
                    server_default="0",
                )
            )
        if "skipped_by_reason_json" not in columns:
            batch_op.add_column(
                sa.Column(
                    "skipped_by_reason_json",
                    sa.Text,
                    nullable=False,
                    server_default="{}",
                )
            )
        if "risk_prevented_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "risk_prevented_count",
                    sa.Integer,
                    nullable=False,
                    server_default="0",
                )
            )

    conn.execute(
        text(
            "UPDATE simulation_runs "
            "SET strategy_name = rule_name "
            "WHERE strategy_name IS NULL OR strategy_name = ''"
        )
    )

    if "simulation_skipped_orders" not in tables:
        op.create_table(
            "simulation_skipped_orders",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "run_id",
                sa.Integer,
                sa.ForeignKey("simulation_runs.id"),
                nullable=False,
            ),
            sa.Column("event_id", sa.Integer, nullable=False),
            sa.Column("token_id", sa.Text, nullable=False),
            sa.Column("condition_id", sa.Text, nullable=True),
            sa.Column("strategy_name", sa.Text, nullable=False),
            sa.Column("base_rule", sa.Text, nullable=False),
            sa.Column("side", sa.Text, nullable=True),
            sa.Column("order_price", sa.Text, nullable=True),
            sa.Column("order_size", sa.Text, nullable=True),
            sa.Column("skipped_reason", sa.Text, nullable=False),
            sa.Column("context_status", sa.Text, nullable=True),
            sa.Column("book_age_s", sa.Integer, nullable=True),
            sa.Column("created_ts", sa.Integer, nullable=False),
        )
        op.create_index(
            "ix_simulation_skipped_orders_run",
            "simulation_skipped_orders",
            ["run_id"],
        )

    indexes = {idx["name"] for idx in inspector.get_indexes("simulation_runs")}
    if "ix_simulation_runs_wallet_strategy" not in indexes:
        op.create_index(
            "ix_simulation_runs_wallet_strategy",
            "simulation_runs",
            ["wallet", "strategy_name", "scenario"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())

    if "simulation_skipped_orders" in tables:
        op.drop_index(
            "ix_simulation_skipped_orders_run",
            table_name="simulation_skipped_orders",
        )
        op.drop_table("simulation_skipped_orders")

    if "simulation_runs" not in tables:
        return

    columns = {col["name"] for col in inspector.get_columns("simulation_runs")}
    with op.batch_alter_table("simulation_runs") as batch_op:
        if "risk_prevented_count" in columns:
            batch_op.drop_column("risk_prevented_count")
        if "skipped_by_reason_json" in columns:
            batch_op.drop_column("skipped_by_reason_json")
        if "skipped_orders_count" in columns:
            batch_op.drop_column("skipped_orders_count")
        if "strategy_name" in columns:
            batch_op.drop_column("strategy_name")
