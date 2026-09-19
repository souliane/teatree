# test-path: cross-cutting
"""The lock-derived SBOM is regenerated and staged in the SAME commit that stages the lock.

``dist/sbom.json`` is generated from ``uv.lock`` and committed, so a commit that moves a
dependency without regenerating it lands a stale SBOM and CI's ``sbom`` job (regenerate +
``git diff --exit-code``) goes red by construction. ``scripts/hooks/generate_sbom.py`` closes
that gap by reusing the two pieces that already exist: the shipped ``generate_sbom.sh`` stays
the single definition of the SBOM's bytes (CI's ``sbom`` job and
``.github/workflows/uv-lock-upgrade.yml`` call it unchanged), and ``generated_doc_staging``
stays the single staging chokepoint — the pattern ``generate-cli-reference`` established
(souliane/teatree#2599).

Staging alone is not enough, because git cannot re-include a file under an excluded
DIRECTORY: the ``!dist/sbom.json`` negation behind a bare ``dist/`` is dead, and
``git add -- dist/sbom.json`` refuses it even though it is tracked. ``dist/*`` is the shape
that keeps ``dist/`` build output ignored while the tracked SBOM stays stageable.

The generator subprocess is the only thing stubbed: every test drives a real git checkout
with the repo's own ``.gitignore``, so the staging path under test is the production one.

See souliane/teatree#2663.
"""

import re
from pathlib import Path
from typing import cast

import generate_sbom
import pytest
import yaml

from tests._git_repo import make_git_repo, run_git, run_git_captured

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_HOOK_ID = "cyclonedx-sbom"
_SBOM_REL = "dist/sbom.json"

_OLD_SBOM = b'{"components": [{"name": "old"}], "version": 1}\n'
_NEW_SBOM = b'{"components": [{"name": "new"}], "version": 2}\n'

# A stand-in for the real generator: write canned bytes, exit with a canned code. Neither
# file present means "write nothing, exit 0", which is the idempotent no-change case.
_STUB_GENERATOR = '''#!/usr/bin/env python3
"""Stub generator: the third-party tool boundary, and the ONLY thing these tests stub."""

import pathlib

content = pathlib.Path("stub-sbom.json")
if content.is_file():
    out = pathlib.Path("dist/sbom.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content.read_bytes())

exit_code = pathlib.Path("stub-exit.txt")
raise SystemExit(int(exit_code.read_text().strip()) if exit_code.is_file() else 0)
'''


@pytest.fixture
def sbom_repo(tmp_path: Path) -> Path:
    """A real checkout: a TRACKED ``dist/sbom.json``, the repo's real ``.gitignore``.

    The SBOM is force-added so it is tracked whichever ignore shape ``.gitignore`` carries —
    which is the production situation (the file is committed) and what makes the ignore-shape
    test measure the ignore rule rather than what a first ``git add`` happened to pick up.
    """
    root = make_git_repo(tmp_path / "repo")
    (root / ".gitignore").write_text((_REPO_ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "dist").mkdir()
    (root / _SBOM_REL).write_bytes(_OLD_SBOM)
    (root / "dist" / "teatree-0.0.0-py3-none-any.whl").write_bytes(b"build output, never committed")
    generator = root / "scripts" / "hooks" / "generate_sbom.sh"
    generator.parent.mkdir(parents=True)
    generator.write_text(_STUB_GENERATOR, encoding="utf-8")
    generator.chmod(0o755)
    run_git(root, "add", "-A")
    run_git(root, "add", "-f", _SBOM_REL)
    run_git(root, "commit", "-q", "-m", "initial")
    return root


def _staged(repo: Path) -> list[str]:
    """The paths the in-progress commit would record — the INDEX, not the working tree."""
    return [line for line in run_git(repo, "diff", "--cached", "--name-only").splitlines() if line]


def _hook(hook_id: str) -> dict[str, object] | None:
    config = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == hook_id:
                return hook
    return None


class TestRegenerateAndStage:
    def test_a_changed_sbom_is_regenerated_and_staged(self, sbom_repo, capsys):
        (sbom_repo / "stub-sbom.json").write_bytes(_NEW_SBOM)

        assert generate_sbom.main(repo_root=sbom_repo) == 0

        assert (sbom_repo / _SBOM_REL).read_bytes() == _NEW_SBOM
        assert _SBOM_REL in _staged(sbom_repo)
        assert "Updated dist/sbom.json" in capsys.readouterr().out

    def test_an_unchanged_sbom_stages_nothing_and_says_nothing(self, sbom_repo, capsys):
        """The hook runs on every lock-touching commit, so a fresh SBOM must cost no noise."""
        (sbom_repo / "stub-sbom.json").write_bytes(_OLD_SBOM)

        assert generate_sbom.main(repo_root=sbom_repo) == 0

        assert _staged(sbom_repo) == []
        assert capsys.readouterr().out == ""

    def test_no_stage_regenerates_without_staging(self, sbom_repo, monkeypatch, capsys):
        """``SBOM_NO_STAGE`` is CI's arm: a working-tree diff must still catch staleness."""
        monkeypatch.setenv("SBOM_NO_STAGE", "1")
        (sbom_repo / "stub-sbom.json").write_bytes(_NEW_SBOM)

        assert generate_sbom.main(repo_root=sbom_repo) == 0

        assert (sbom_repo / _SBOM_REL).read_bytes() == _NEW_SBOM
        assert _staged(sbom_repo) == []
        assert _SBOM_REL in run_git(sbom_repo, "diff", "--name-only")
        assert capsys.readouterr().out == ""

    def test_a_failing_generator_fails_the_hook_and_stages_nothing(self, sbom_repo, capsys):
        """Fail loud: a broken generator must refuse the commit, never stage a guess."""
        (sbom_repo / "stub-exit.txt").write_text("3", encoding="utf-8")
        (sbom_repo / "stub-sbom.json").write_bytes(_NEW_SBOM)

        assert generate_sbom.main(repo_root=sbom_repo) == 3

        assert _staged(sbom_repo) == []
        assert "generate_sbom.sh" in capsys.readouterr().err


class TestTheShippedIgnoreShapeKeepsTheSbomStageable:
    """``dist/`` excludes the DIRECTORY, so git never re-includes ``!dist/sbom.json`` under it.

    This is the guard the whole hook rests on: staging a tracked SBOM only works at all
    because ``.gitignore`` says ``dist/*``. Measured, not assumed — a bare ``dist/`` refuses
    ``git add -- dist/sbom.json`` with "paths are ignored" even for a tracked file.
    """

    def test_a_modified_sbom_can_be_staged(self, sbom_repo):
        (sbom_repo / _SBOM_REL).write_bytes(_NEW_SBOM)

        assert run_git_captured(sbom_repo, "add", "--", _SBOM_REL).returncode == 0
        assert _SBOM_REL in _staged(sbom_repo)

    def test_build_output_under_dist_stays_ignored(self, sbom_repo):
        assert run_git(sbom_repo, "check-ignore", "dist/teatree-0.0.0-py3-none-any.whl", check=False) == (
            "dist/teatree-0.0.0-py3-none-any.whl"
        )


class TestHookWiring:
    def test_the_hook_runs_on_commit_over_the_lock_and_pyproject(self):
        """RED before the wiring: the hook is ``stages: [manual]`` and invisible to a commit."""
        hook = _hook(_HOOK_ID)

        assert hook is not None, f"hook {_HOOK_ID!r} is missing from .pre-commit-config.yaml"
        stages = cast("list[str]", hook.get("stages") or [])
        assert "manual" not in [str(stage) for stage in stages]
        assert hook.get("pass_filenames") is False
        assert str(hook["entry"]).endswith("scripts/hooks/generate_sbom.py")
        pattern = str(hook["files"])
        assert re.search(pattern, "uv.lock")
        assert re.search(pattern, "pyproject.toml")
        assert not re.search(pattern, "src/teatree/cli/app.py")

    def test_the_wrapper_and_the_generator_it_drives_both_exist(self):
        """The wrapper is a shell-out; the .sh stays the single generator (CI calls it too)."""
        assert Path("scripts/hooks/generate_sbom.sh") == generate_sbom.GENERATOR_REL
        assert (_REPO_ROOT / "scripts/hooks/generate_sbom.py").is_file()
        assert (_REPO_ROOT / generate_sbom.GENERATOR_REL).is_file()
