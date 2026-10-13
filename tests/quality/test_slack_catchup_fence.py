# test-path: cross-cutting
"""The Slack catch-up package is fenced: one importer, one interface, a narrow outbound edge set.

tach enforces the declared fence; the static scan covers what tach cannot see (an import
under ``TYPE_CHECKING``, an import from a root-module file no declared module owns, and a
module path handed to ``importlib``).
"""

import ast
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

import teatree.slack_catchup
from teatree.hooks.portable.check_module_health import MAX_MODULE_FUNCTIONS, _public_module_functions

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "teatree"
_PACKAGE = "teatree.slack_catchup"
_ONLY_IMPORTER = _SRC / "loops" / "slack_catchup_job.py"


def _src_files() -> Iterator[Path]:
    package_dir = _SRC / "slack_catchup"
    for path in sorted(_SRC.rglob("*.py")):
        if package_dir not in path.parents:
            yield path


def _names_the_package(dotted: str) -> bool:
    return dotted == _PACKAGE or dotted.startswith(f"{_PACKAGE}.")


def _references(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(_names_the_package(alias.name) for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if _names_the_package(node.module):
                return True
            if any(_names_the_package(f"{node.module}.{alias.name}") for alias in node.names):
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _names_the_package(node.value):
            return True
    return False


def _tach_config() -> dict:
    return tomllib.loads((_REPO_ROOT / "tach.toml").read_text(encoding="utf-8"))


def test_the_only_file_naming_the_package_is_the_job() -> None:
    importers = [path for path in _src_files() if _references(ast.parse(path.read_text(encoding="utf-8")))]

    assert importers == [_ONLY_IMPORTER]


@pytest.mark.parametrize(
    "source",
    [
        "import teatree.slack_catchup\n",
        "from teatree import slack_catchup\n",
        "from teatree.slack_catchup.replay import catch_up\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from teatree.slack_catchup import catch_up\n",
        "import importlib\nimportlib.import_module('teatree.slack_catchup')\n",
    ],
)
def test_every_import_form_counts_as_naming_the_package(source: str) -> None:
    assert _references(ast.parse(source))


def test_a_prose_mention_does_not_count() -> None:
    assert not _references(ast.parse('"""Only :mod:`teatree.slack_catchup` may replay Slack."""\n'))


def test_tach_declaration_is_pinned() -> None:
    config = _tach_config()
    [module] = [m for m in config["modules"] if m["path"] == _PACKAGE]
    [interface] = [i for i in config["interfaces"] if _PACKAGE in i["from"]]

    assert "layer" not in module
    assert module["visibility"] == ["teatree.loops.slack_catchup_job"]
    assert module["depends_on"] == ["teatree.backends.slack", "teatree.types"]
    assert sorted(interface["expose"]) == sorted(teatree.slack_catchup.__all__) == ["CatchUpSlice", "catch_up"]


def test_tach_rejects_a_foreign_importer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(_REPO_ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(_REPO_ROOT / "tach.toml", repo / "tach.toml")
    listen = repo / "src" / "teatree" / "cli" / "slack" / "listen.py"
    listen.write_text(
        listen.read_text(encoding="utf-8") + "\nfrom teatree.slack_catchup import catch_up as _probe\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "tach", "check"], cwd=repo, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert f"cannot depend on '{_PACKAGE}'" in result.stdout + result.stderr


def test_no_src_call_to_fetch_dms() -> None:
    callers = [
        path
        for path in _SRC.rglob("*.py")
        if any(
            isinstance(node, ast.Attribute) and node.attr == "fetch_dms"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]

    assert callers == []


def test_public_function_counts_stay_inside_the_module_health_cap() -> None:
    def public(relative: str) -> set[str]:
        return set(_public_module_functions((_SRC / relative).read_text(encoding="utf-8")))

    heartbeat = {"heartbeat_path", "write_heartbeat", "read_heartbeat", "heartbeat_is_stale"}
    receiver = public("backends/slack/receiver.py")

    assert public("backends/slack/listener_heartbeat.py") == heartbeat
    assert not receiver & heartbeat
    assert len(receiver) == 9
    assert len(public("loops/timer_reconciler.py")) <= MAX_MODULE_FUNCTIONS
