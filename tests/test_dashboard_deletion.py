"""Phase 16 deletion test (ADR 0004): the core test suite must pass with
`apps/dashboard` renamed away, proving the dashboard is truly disposable."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "apps" / "dashboard"
DISABLED_DIR = REPO_ROOT / "apps" / "dashboard.disabled"


def test_core_suite_passes_without_dashboard():
    assert DASHBOARD_DIR.is_dir(), "apps/dashboard does not exist"
    shutil.move(str(DASHBOARD_DIR), str(DISABLED_DIR))
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-x",
                "-q",
                "--ignore=tests/test_dashboard_deletion.py",
                "--ignore=tests/test_dashboard_boundary.py",
                "--ignore=tests/test_api_facade.py",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        shutil.move(str(DISABLED_DIR), str(DASHBOARD_DIR))

    assert result.returncode == 0, (
        f"Core test suite failed with apps/dashboard removed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
