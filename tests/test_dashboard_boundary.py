"""Phase 16 import-boundary test: `apps/dashboard/**` may only import
`pmresearch.api` (never any other `pmresearch` submodule), and `pmresearch/`
must never import streamlit — the dependency arrow only goes one way."""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "apps" / "dashboard"
PMRESEARCH_DIR = REPO_ROOT / "pmresearch"


def _iter_py_files(root: pathlib.Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _check_dashboard_file(path: pathlib.Path) -> list[str]:
    violations = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "pmresearch" or name == "pmresearch.api":
                    continue
                if name.startswith("pmresearch"):
                    violations.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pmresearch":
                # `from pmresearch import api` is allowed; `from pmresearch import
                # anything_else` is not.
                bad_names = [a.name for a in node.names if a.name != "api"]
                if bad_names:
                    violations.append(f"from pmresearch import {', '.join(bad_names)}")
            elif module == "pmresearch.api":
                continue
            elif module.startswith("pmresearch"):
                violations.append(f"from {module} import ...")
    return violations


def test_dashboard_only_imports_pmresearch_api():
    assert DASHBOARD_DIR.is_dir(), "apps/dashboard does not exist"
    offenders: list[str] = []
    for path in _iter_py_files(DASHBOARD_DIR):
        violations = _check_dashboard_file(path)
        for violation in violations:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {violation}")
    assert not offenders, "Dashboard files import pmresearch outside the api façade:\n" + "\n".join(
        offenders
    )


def test_pmresearch_never_imports_streamlit():
    offenders: list[str] = []
    for path in _iter_py_files(PMRESEARCH_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "streamlit" or alias.name.startswith("streamlit."):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "streamlit" or module.startswith("streamlit."):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: from {module} import ...")
    assert not offenders, "pmresearch imports streamlit, breaking ADR 0004:\n" + "\n".join(offenders)
