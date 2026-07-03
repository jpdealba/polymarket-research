"""Alembic entry points shared by the CLI and the collector app.

Repo layout is fixed (alembic/ and alembic.ini live at the repo root, next to
the pmresearch/ package) so the path is derived from this file's location
rather than the current working directory.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from ..config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(settings: Settings) -> AlembicConfig:
    cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return cfg


def upgrade_to_head(settings: Settings) -> None:
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")


def downgrade(settings: Settings, revision: str) -> None:
    command.downgrade(alembic_config(settings), revision)


def current_revision(settings: Settings, verbose: bool = False) -> None:
    command.current(alembic_config(settings), verbose=verbose)
