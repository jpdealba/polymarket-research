import pytest

from pmresearch.config import Settings, ensure_data_dirs
from pmresearch.db.engine import get_session_factory
from pmresearch.db.migrations import upgrade_to_head


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path, log_level="INFO", rpc_url="", rclone_remote="")
    ensure_data_dirs(s)
    upgrade_to_head(s)
    return s


@pytest.fixture
def session(settings):
    factory = get_session_factory(settings)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
