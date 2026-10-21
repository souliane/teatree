# test-path: cross-cutting
"""A module that depends only on a forge's read interface cannot reach a write.

tach refuses a non-exposed name imported from a reads module. It admits what this scan
refuses: absolute and relative reads-module bindings, a submodule path, a
``TYPE_CHECKING`` import and supported dynamic import literals. The reads modules
bind no write-capable names, so binding a module object in any form reaches no write.
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
from importlib.util import resolve_name
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
        "_run_gh",
        "run_gh_unchecked",
        "gh_runner",
        "gh_merge_rpc",
        "GhMergeRpc",
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


def _literal_argument(call: ast.Call, position: int, keyword: str) -> str | int | None:
    argument = (
        call.args[position]
        if len(call.args) > position
        else next((item.value for item in call.keywords if item.arg == keyword), None)
    )
    value = argument.value if isinstance(argument, ast.Constant) else None
    return value if isinstance(value, (str, int)) else None


def _dynamic_read_module(call: ast.Call, package: str, importlib_names: set[str], function_names: set[str]) -> str:
    name = _literal_argument(call, 0, "name")
    if not isinstance(name, str):
        return ""
    if isinstance(call.func, ast.Name) and call.func.id == "__import__":
        level = _literal_argument(call, 4, "level")
        if isinstance(level, int) and level > 0:
            return _names_a_read_module(resolve_name("." * level + name, package))
        return ""
    if not (
        (isinstance(call.func, ast.Name) and call.func.id in function_names)
        or (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "import_module"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in importlib_names
        )
    ):
        return ""
    if not name.startswith("."):
        return ""
    supplied_package = _literal_argument(call, 1, "package")
    resolved = resolve_name(name, supplied_package if isinstance(supplied_package, str) else package)
    return _names_a_read_module(resolved)


def reaches_past_the_interface(tree: ast.AST, exposed: Mapping[str, frozenset[str]], package: str) -> list[str]:
    """Every reference in *tree* to a reads module other than importing one of its exposed names."""
    found: list[str] = []
    importlib_names = {
        alias.asname or "importlib"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "importlib"
    }
    import_module_names = {
        alias.asname or "import_module"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib"
        for alias in node.names
        if alias.name == "import_module"
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _names_a_read_module(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
            if module in exposed:
                found += [f"{module}.{a.name}" for a in node.names if a.name not in exposed[module]]
            elif _names_a_read_module(module):
                found.append(module)
            else:
                found += [f"{module}.{a.name}" for a in node.names if f"{module}.{a.name}" in exposed]
        elif isinstance(node, ast.Call):
            if module := _dynamic_read_module(node, package, importlib_names, import_module_names):
                found.append(module)
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

    assert reaches_past_the_interface(
        ast.parse(f"from {module} import submit_pr_review, _gh_api_post\nfrom {module} import *\n"),
        _exposed(),
        "teatree.backends",
    ) == [f"{module}.submit_pr_review", f"{module}._gh_api_post", f"{module}.*"]

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
    bound = vars(importlib.import_module(module))
    assert bound.keys().isdisjoint(_forge_writes())
    for name, value in bound.items():
        assert getattr(value, "__name__", "") not in _forge_writes(), f"{module}.{name}"
        if inspect.ismodule(value) or (
            (inspect.isclass(value) or inspect.isfunction(value)) and value.__module__ != module
        ):
            for attribute in dir(value):
                assert attribute not in _forge_writes(), f"{module}.{name}.{attribute}"
                member = getattr(value, attribute)
                assert getattr(member, "__name__", "") not in _forge_writes(), f"{module}.{name}.{attribute}"


def test_no_module_reaches_past_a_read_interface() -> None:
    read_files = {_module_file(module) for module in READ_MODULES}
    offenders = {
        str(path.relative_to(REPO_ROOT)): found
        for path, tree in src_modules()
        if path not in read_files
        and (
            found := reaches_past_the_interface(
                tree, _exposed(), ".".join(path.parent.relative_to(SRC_DIR.parent).parts)
            )
        )
    }

    assert offenders == {}


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("import teatree.backends.github.reads\n", "teatree.backends"),
        ("from teatree.backends.gitlab import reads as gitlab_reads\n", "teatree.backends"),
        ("from teatree.backends.github.reads.sub import anything\n", "teatree.backends"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from teatree.backends.gitlab import reads\n",
            "teatree.backends",
        ),
        ("import importlib\nimportlib.import_module('teatree.backends.gitlab.reads')\n", "teatree.backends"),
        ("from .github import reads\n", "teatree.backends"),
        ("from . import reads\n", "teatree.backends.github"),
        (
            "from importlib import import_module\nimport_module('.reads', 'teatree.backends.github')\n",
            "teatree.backends.github",
        ),
        ("import importlib\nimportlib.import_module('.reads', 'teatree.backends.github')\n", "teatree.backends"),
        (
            "import importlib\nimportlib.import_module(name='.reads', package='teatree.backends.github')\n",
            "teatree.backends",
        ),
        ("from importlib import import_module as load\nload('.github.reads', __package__)\n", "teatree.backends"),
        ("__import__('github.reads', globals(), None, ['x'], 1)\n", "teatree.backends"),
    ],
)
def test_every_form_that_binds_more_than_an_exposed_read_counts(source: str, package: str) -> None:
    assert reaches_past_the_interface(ast.parse(source), _exposed(), package)
