"""No leak, overlay or allow value of the term registry appears in the repository's value-free roots.

A finding is printed as ``path:line: class#index``, never by value. The only exemption is the repository
owner's login, read from the ``origin`` remote, and only where it occurs exactly.
"""

import json
import secrets
from pathlib import Path
from typing import Any, cast

import click
import pytest
import yaml
from typer.testing import CliRunner

from teatree.cli.banned_terms import banned_terms_app
from teatree.core.banned_terms_tree import scan_registry_values
from teatree.hooks.banned_term_registry import load_registry
from teatree.utils.run import run_checked

ROOT = Path(__file__).resolve().parents[2]
CLASSES = "leak,overlay,allow"
OWNER = "acme-owner"


def _value(kind: str) -> str:
    return f"widget-{kind}-{secrets.token_hex(3)}"


def _git(repo: Path, *args: str) -> None:
    run_checked(["git", "-C", str(repo), *args])


type Planted = tuple[Path, dict[str, list[str]]]


@pytest.fixture
def planted(tmp_path: Path) -> Planted:
    registry = {
        "leak": [_value("leak"), _value("leak")],
        "prose_collider": [_value("prose")],
        "tone": [],
        "overlay": [_value("overlay"), _value("overlay")],
        "allow": [OWNER, _value("allow")],
    }
    files = {
        "src/pkg/module.py": f"NAME = '{registry['leak'][0]}'\n",
        "scripts/run.sh": f"echo {registry['overlay'][0]}\n",
        "tests/test_module.py": f"# owner page: github.example/{OWNER}/widget\nVALUE = '{registry['allow'][1]}'\n",
        "docs/guide.md": f"Deploy {OWNER}-bot today.\nThen {registry['leak'][1]}.\n",
        ".github/workflows/ci.yml": f"name: {registry['overlay'][1]}\n",
        "src/pkg/prose.py": f"TEXT = '{registry['prose_collider'][0]}'\n",
        "NOTICE": f"Outside the value-free roots: {registry['leak'][0]}\n",
    }
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    (repo / "src" / "pkg" / "blob.dat").write_bytes(b"\xff\xfe" + registry["leak"][0].encode())
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", f"https://github.example/{OWNER}/widget.git")
    _git(repo, "add", ".")
    return repo, registry


def _scan(repo: Path, registry: dict[str, list[str]] | None, *extra: str) -> tuple[int, str]:
    env = {"TEATREE_TERM_REGISTRY": json.dumps(registry) if registry is not None else ""}
    result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo), *extra], env=env)
    return result.exit_code, click.unstyle(result.output)


def test_a_value_in_each_root_is_found_and_named_by_class_only(planted: Planted) -> None:
    repo, registry = planted

    exit_code, output = _scan(repo, registry, "--classes", CLASSES)

    assert exit_code == 1, output
    assert sorted(line.strip() for line in output.splitlines() if ": " in line and "#" in line) == [
        ".github/workflows/ci.yml:1: overlay#1",
        "docs/guide.md:1: allow#0",
        "docs/guide.md:2: leak#1",
        "scripts/run.sh:1: overlay#0",
        "src/pkg/module.py:1: leak#0",
        "tests/test_module.py:2: allow#1",
    ]
    assert not [value for values in registry.values() for value in values if value in output]


def test_the_default_scan_keeps_reading_only_the_leak_class(planted: Planted) -> None:
    repo, registry = planted

    exit_code, output = _scan(repo, registry)

    assert exit_code == 1, output
    assert "leak#0" in output
    assert "overlay#" not in output


@pytest.mark.parametrize("registered", [False, True])
def test_no_value_to_scan_is_misconfigured(planted: Planted, *, registered: bool) -> None:
    repo, registry = planted

    exit_code, output = _scan(repo, registry if registered else None, "--classes", "tone" if registered else CLASSES)

    assert exit_code == 2
    assert "MISCONFIGURED" in output


@pytest.mark.parametrize("classes", ["leak,nonsense", ","])
def test_a_class_list_naming_no_registry_class_is_a_usage_error(planted: Planted, classes: str) -> None:
    repo, registry = planted

    assert _scan(repo, registry, "--classes", classes)[0] == 2


@pytest.fixture(scope="module")
def _readable_registry() -> str | None:
    registry = load_registry()
    return None if registry is None else json.dumps(registry)


@pytest.fixture
def real_registry(_readable_registry: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if _readable_registry is None:
        pytest.skip("no term registry is readable on this host")
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", _readable_registry)


@pytest.mark.usefixtures("real_registry")
def test_the_repository_holds_no_registry_value() -> None:
    result = scan_registry_values(ROOT, ("leak", "overlay", "allow"))

    assert [finding.render() for finding in result.findings] == []


def test_the_required_tree_job_runs_this_scan() -> None:
    workflow = cast("dict[str, Any]", yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text()))
    steps = [step for step in workflow["jobs"]["banned-terms-tree"]["steps"] if isinstance(step, dict)]
    scans = [step for step in steps if f"scan-tree --repo-root . --classes {CLASSES}" in str(step.get("run", ""))]

    assert len(scans) == 1
    assert scans[0]["env"] == {"TEATREE_TERM_REGISTRY": "${{ secrets.TEATREE_TERM_REGISTRY }}"}
    assert "github.event_name != 'pull_request'" in scans[0]["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in scans[0]["if"]
