"""Thin collector entrypoint: logging, migrations, then an APScheduler loop.

Phase 0: no jobs are registered yet (no source adapters exist). This proves
the container boots, applies migrations, and stays up against the mounted
/data volume.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from pmresearch.config import ensure_data_dirs, get_settings
from pmresearch.db.migrations import upgrade_to_head
from pmresearch.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    logger.info("Starting collector; data_dir=%s", settings.data_dir)

    upgrade_to_head(settings)
    logger.info("Migrations applied.")

    scheduler = BlockingScheduler()
    logger.info("Collector scheduler started (no jobs registered yet).")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Collector shutting down.")


if __name__ == "__main__":
    main()
