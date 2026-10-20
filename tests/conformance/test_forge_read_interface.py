# test-path: cross-cutting
"""A module that depends only on a forge's read interface cannot reach a write.

tach refuses a non-exposed name imported from a reads module. It admits what this scan
refuses: binding the reads module object (its attributes reach every name it binds), a
submodule path, a ``TYPE_CHECKING`` import and an ``importlib`` string. The scan also
proves each reads module exposes, and itself touches, no write.
"""

import ast
import importlib
import inspect
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path

import pytest

from teatree.quality.chokepoints import load_registry
from tests.conformance._src_tree import REPO_ROOT, SRC_DIR, src_modules

READ_MODULES = ("teatree.backends.github.reads", "teatree.backends.gitlab.reads")

#: The module a probe import is planted in: one whose tach entry may depend on the reads module.
_PROBE_SITES = {
    "teatree.backends.github.reads": Path("src/teatree/backends/github/client.py"),
    "teatree.backends.gitlab.reads": Path("src/teatree/backends/gitlab/client.py"),
}

_READ_VERBS = ("get_", "list_", "fetch_", "find_", "count_", "search_")

#: The transport verbs that carry a request body; a generic ``gh`` runner is allowed only without a method switch.
_WRITE_TRANSPORT = frozenset(
    {
        "_gh_api_write",
        "_gh_api_post",
        "_gh_api_patch",
        "post_json",
        "post_status",
        "put_json",
        "put_response",
        "put_status",
        "delete",
        "upload_file",
    }
)

_GH_METHOD_SWITCHES = frozenset({"--method", "-X", "--input"})

_WRITE_CHOKEPOINTS = frozenset({"forge-comment-write-seam", "merge-message-seam"})


def _forge_writes() -> frozenset[str]:
    registered = {attr for entry in load_registry() if entry.id in _WRITE_CHOKEPOINTS for attr in entry.protected_attrs}
    return frozenset(registered) | _WRITE_TRANSPORT


@cache
def _exposed() -> Mapping[str, frozenset[str]]:
    config = tomllib.loads((REPO_ROOT / "tach.toml").read_text(encoding="utf-8"))
    return {
        module: frozenset(name for entry in config["interfaces"] if module in entry["from"] for name in entry["expose"])
        for module in READ_MODULES
    }


def _names_a_read_module(dotted: str) -> str:
    return next((m for m in READ_MODULES if dotted == m or dotted.startswith(f"{m}.")), "")


def reaches_past_the_interface(tree: ast.AST, exposed: Mapping[str, frozenset[str]]) -> list[str]:
    """Every reference in *tree* to a reads module other than importing one of its exposed names."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _names_a_read_module(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in exposed:
                found += [f"{node.module}.{a.name}" for a in node.names if a.name not in exposed[node.module]]
            elif _names_a_read_module(node.module):
                found.append(node.module)
            else:
                found += [f"{node.module}.{a.name}" for a in node.names if f"{node.module}.{a.name}" in exposed]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _names_a_read_module(node.value):
            found.append(node.value)
    return found


def _module_file(module: str) -> Path:
    return SRC_DIR.parent / f"{module.replace('.', '/')}.py"


@pytest.fixture(scope="module")
def tach_output_with_write_probes(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, int]:
    repo = tmp_path_factory.mktemp("tach-probe")
    shutil.copytree(REPO_ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO_ROOT / "tach.toml", repo / "tach.toml")
    for module, site in _PROBE_SITES.items():
        target = repo / site
        target.write_text(
            target.read_text(encoding="utf-8") + f"\nfrom {module} import submit_pr_review as _probe\n",
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, "-m", "tach", "check"], cwd=repo, capture_output=True, text=True, check=False
    )
    return result.stdout + result.stderr, result.returncode


@pytest.mark.parametrize("module", READ_MODULES)
def test_reads_module_exposes_no_write(module: str, tach_output_with_write_probes: tuple[str, int]) -> None:
    output, returncode = tach_output_with_write_probes
    assert returncode != 0
    assert f"'{module}.submit_pr_review' is not part of the public interface for '{module}'" in output
    assert output.count("[FAIL]") == len(_PROBE_SITES), output

    assert reaches_past_the_interface(ast.parse(f"from {module} import submit_pr_review\n"), _exposed()) == [
        f"{module}.submit_pr_review"
    ]

    imported = importlib.import_module(module)
    assert _exposed()[module], module
    for name in _exposed()[module]:
        obj = getattr(imported, name)
        assert inspect.isfunction(obj), name
        assert obj.__module__ == module, name
        assert name.startswith(_READ_VERBS), name
        assert name not in _forge_writes(), name


@pytest.mark.parametrize("module", READ_MODULES)
def test_interface_is_exactly_the_module_all(module: str) -> None:
    config = tomllib.loads((REPO_ROOT / "tach.toml").read_text(encoding="utf-8"))
    [declared] = [entry for entry in config["modules"] if entry["path"] == module]
    [interface] = [entry for entry in config["interfaces"] if module in entry["from"]]

    assert declared["layer"] == "domain"
    assert interface["from"] == [module]
    assert sorted(interface["expose"]) == sorted(importlib.import_module(module).__all__)


@pytest.mark.parametrize("module", READ_MODULES)
def test_reads_module_touches_no_write(module: str) -> None:
    tree = ast.parse(_module_file(module).read_text(encoding="utf-8"))
    nodes = list(ast.walk(tree))
    touched = {alias.name for node in nodes if isinstance(node, ast.ImportFrom) for alias in node.names}
    touched |= {node.id for node in nodes if isinstance(node, ast.Name)}
    touched |= {node.attr for node in nodes if isinstance(node, ast.Attribute)}
    literals = {node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert touched & _forge_writes() == set()
    assert literals & _GH_METHOD_SWITCHES == set()


def test_no_module_reaches_past_a_read_interface() -> None:
    read_files = {_module_file(module) for module in READ_MODULES}
    offenders = {
        str(path.relative_to(REPO_ROOT)): found
        for path, tree in src_modules()
        if path not in read_files and (found := reaches_past_the_interface(tree, _exposed()))
    }

    assert offenders == {}


@pytest.mark.parametrize("module", READ_MODULES)
def test_the_forge_client_reads_through_its_read_interface(module: str) -> None:
    tree = ast.parse((REPO_ROOT / _PROBE_SITES[module]).read_text(encoding="utf-8"))

    assert any(isinstance(node, ast.ImportFrom) and node.module == module for node in ast.walk(tree))


@pytest.mark.parametrize(
    "source",
    [
        "import teatree.backends.github.reads\n",
        "from teatree.backends.github import reads\n",
        "from teatree.backends.gitlab import reads as gitlab_reads\n",
        "from teatree.backends.github.reads import submit_pr_review\n",
        "from teatree.backends.github.reads import *\n",
        "from teatree.backends.github.reads import _gh_api_post\n",
        "from teatree.backends.github.reads.sub import anything\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from teatree.backends.gitlab import reads\n",
        "import importlib\nimportlib.import_module('teatree.backends.gitlab.reads')\n",
    ],
)
def test_every_form_that_binds_more_than_an_exposed_read_counts(source: str) -> None:
    assert reaches_past_the_interface(ast.parse(source), _exposed())


def test_importing_an_exposed_read_does_not_count() -> None:
    name = min(_exposed()["teatree.backends.gitlab.reads"])
    source = f"from teatree.backends.gitlab.reads import {name}\n"

    assert reaches_past_the_interface(ast.parse(source), _exposed()) == []


def test_a_prose_mention_does_not_count() -> None:
    source = '"""Reads go through :mod:`teatree.backends.github.reads`."""\n'

    assert reaches_past_the_interface(ast.parse(source), _exposed()) == []
