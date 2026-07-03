from __future__ import annotations

from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

config = context.config

# Deliberately no logging.config.fileConfig(config.config_file_name) here: it
# would reset the root logger's handlers/level (from alembic.ini's own
# logging section), clobbering pmresearch.logging_setup's console+file
# handlers for the rest of the process (the collector keeps running long
# after migrations finish). Alembic's own loggers propagate to root and are
# handled by whatever pmresearch.logging_setup.setup_logging() configured.

# Callers (pmresearch.db.migrations) set sqlalchemy.url on the Config object
# from an explicit Settings instance before invoking Alembic. Only fall back
# to env-derived settings for a bare `alembic` CLI invocation, so this module
# never has two independent, potentially divergent ideas of where the DB is.
if not config.get_main_option("sqlalchemy.url"):
    from pmresearch.config import get_settings

    config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_url)

# No SQLAlchemy models yet (Phase 0 has no business tables); migrations are
# hand-written and unchecked against metadata.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    db_path = Path(make_url(config.get_main_option("sqlalchemy.url")).database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLite's transactional_ddl=False means Alembic's begin_transaction()
        # above is a no-op; the DBAPI connection still autobegins its own
        # transaction on first execute, which must be committed explicitly or
        # it is rolled back when the connection closes.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
