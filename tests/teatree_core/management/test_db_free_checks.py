"""Commands that must answer without a database never open one in their system checks."""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_UNOPENABLE_DB = """
import sys
import teatree.settings as settings
for alias in settings.DATABASES:
    settings.DATABASES[alias]["NAME"] = "/nonexistent/container-only/control-db/db.sqlite3"
"""

_RUN_WITH_UNOPENABLE_DB = (
    _UNOPENABLE_DB
    + """
import django
django.setup()
from django.core.management import ManagementUtility
ManagementUtility(["manage.py", *sys.argv[1:]]).execute()
"""
)

_RUN_HOOK_WITH_UNOPENABLE_DB = (
    _UNOPENABLE_DB
    + """
sys.path.insert(0, "scripts/hooks")
import check_linear_migrations
sys.exit(check_linear_migrations.main())
"""
)


def _run(*argv: str, script: str = _RUN_WITH_UNOPENABLE_DB) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=_REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(_REPO_ROOT), "DJANGO_SETTINGS_MODULE": "teatree.settings"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("generate_all_docs", "--output-dir", "{tmp}"), id="static-docs-generator"),
        pytest.param(("pr", "ensure-pr", "--repo", "{tmp}"), id="pre-push-soft-refusal"),
    ],
)
def test_runs_where_no_database_can_be_opened(argv: tuple[str, ...], tmp_path: Path) -> None:
    result = _run(*(arg.format(tmp=tmp_path) for arg in argv))

    assert "unable to open database file" not in result.stderr
    assert result.returncode == 0, result.stderr


def test_an_ordinary_command_still_opens_the_database_in_its_checks(tmp_path: Path) -> None:
    result = _run("check")

    assert "unable to open database file" in result.stderr


def test_the_migration_graph_hook_runs_where_no_database_can_be_opened() -> None:
    result = _run(script=_RUN_HOOK_WITH_UNOPENABLE_DB)

    assert "unable to open database file" not in result.stderr
    assert result.returncode == 0, result.stderr
