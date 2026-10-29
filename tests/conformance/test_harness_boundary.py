"""Shrink-only census of harness and attended references outside their homes."""

import re
from pathlib import Path

import pytest

from teatree.utils.run import CommandFailedError, run_checked
from tests.quality._deferred_imports import diff_pegs, load_pegs

_ROOT = Path(__file__).resolve().parents[2]
_PEGS = Path(__file__).with_name("harness_boundary_pegs.toml")
_SCOPES = ("src/teatree/", "hooks/", "scripts/", "dev/", "skills/")
_HARNESS = re.compile(
    r"(?i)(?<![a-z0-9])(?:claude[_-](?:agent[_-]sdk|code|sdk|cli|sessions?|plugin)|"
    r"claude[ ]code|claude(?:sdk|agent)|[.]claude[\\/]|codex(?:(?-i:[A-Z][A-Za-z0-9]*)|[_-][a-z0-9_]+)?\b)"
)
_ATTENDED = re.compile(
    r"(?i)(?<![a-z0-9])(?:un)?attended(?=$|[^a-z0-9])|"
    r"(?<!non-)(?<![a-z0-9_])interactive(?=$|[^a-z0-9_])"
)
_HARNESS_HOOKS = frozenset(
    {
        "hooks/codex.json",
        "hooks/hooks.json",
        "hooks/scripts/codex_hook_adapter.py",
        "hooks/scripts/hook_router.py",
        "hooks/scripts/session_lane.py",
        "hooks/scripts/statusline.sh",
    }
)
_ATTENDED_HOOKS = frozenset(
    {
        "hooks/scripts/headless_authoring_gate.py",
        "hooks/scripts/host-pressure-publish.zsh",
        "hooks/scripts/orchestrator_delegation_gate.py",
        "hooks/scripts/orchestrator_investigation_gate.py",
        "hooks/scripts/owner_turn_context.py",
        "hooks/scripts/session_handover_pickup.py",
        "hooks/scripts/standing_directives_delivery.py",
        "hooks/scripts/standing_rules_start.py",
        "hooks/scripts/statusline.sh",
        "hooks/scripts/stranded_work_report.py",
        "hooks/scripts/stranded_work_start.py",
        "hooks/scripts/worker_supervisor.py",
    }
)


def _count_references(path: str, text: str) -> dict[str, int]:
    interactive_home = path.startswith("skills/interactive/") or path == "skills/rules/RULES.md"
    harness_home = (
        interactive_home
        or path in _HARNESS_HOOKS
        or (path.startswith(("src/teatree/agents/claude_", "src/teatree/agents/codex_")) and path.endswith(".py"))
    )
    attended_home = interactive_home or path in _ATTENDED_HOOKS
    source = f"{path}\n{text}"
    return {
        "harness": 0 if harness_home else len(_HARNESS.findall(source)),
        "attended": 0 if attended_home else len(_ATTENDED.findall(source)),
    }


def _scan_references(root: Path, *, minimum_files: int) -> dict[str, dict[str, int]]:
    paths = run_checked(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *_SCOPES]
    ).stdout.split("\0")
    counts: dict[str, dict[str, int]] = {"harness": {}, "attended": {}}
    scanned = 0
    for path in paths:
        if not path or path == "dev/.test_durations":
            continue
        raw = (root / path).read_bytes()
        if b"\0" in raw:
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for family, count in _count_references(path, source).items():
            if count:
                counts[family][path] = count
    if scanned < minimum_files:
        message = f"scan read {scanned} files; expected at least {minimum_files}"
        raise ValueError(message)
    return counts


@pytest.mark.parametrize(
    ("source", "family"),
    [
        ("Claude Code", "harness"),
        ("claude_agent_sdk", "harness"),
        ("CLAUDE_CODE_SESSION_ID", "harness"),
        ("codex_app_server", "harness"),
        ("CodexAppServer", "harness"),
        ("ClaudeSDK", "harness"),
        ("~/.claude/settings.json", "harness"),
        ("attended", "attended"),
        ("unattended", "attended"),
        ("interactive", "attended"),
    ],
)
def test_reference_vocabulary_counts(source: str, family: str) -> None:
    assert _count_references("src/teatree/core/example.py", source)[family] == 1


@pytest.mark.parametrize(
    "source",
    [
        "claude-sonnet-4-5",
        "claude-opus-4",
        "claude-haiku-4",
        "CLAUDE.md",
        "non-interactive",
        "interactive_bash",
        "codexology",
    ],
)
def test_model_and_document_vocabulary_is_spared(source: str) -> None:
    assert _count_references("src/teatree/core/example.py", source) == {"harness": 0, "attended": 0}


def test_generic_hook_does_not_exempt_harness_reference() -> None:
    assert _count_references("hooks/scripts/managed_repo.py", "Claude Code")["harness"] == 1


def test_coordinating_skill_does_not_exempt_attended_reference() -> None:
    assert _count_references("skills/coordinating/SKILL.md", "attended")["attended"] == 1


@pytest.mark.parametrize(
    ("path", "family"),
    [
        ("src/teatree/agents/claude_backend.py", "harness"),
        ("src/teatree/agents/codex_backend.py", "harness"),
        ("hooks/scripts/hook_router.py", "harness"),
        ("skills/interactive/SKILL.md", "attended"),
        ("hooks/scripts/session_handover_pickup.py", "attended"),
        ("skills/rules/RULES.md", "attended"),
    ],
)
def test_allowed_home_is_exempt(path: str, family: str) -> None:
    assert _count_references(path, "Claude Code" if family == "harness" else "attended")[family] == 0


def test_filename_and_untracked_file_are_counted(tmp_path: Path) -> None:
    run_checked(["git", "init", "-q", "-b", "main", str(tmp_path)])
    core = tmp_path / "src" / "teatree" / "core"
    core.mkdir(parents=True)
    (core / "codex_adapter.py").write_text("", encoding="utf-8")
    run_checked(["git", "-C", str(tmp_path), "add", "src/teatree/core/codex_adapter.py"])
    (core / "session.py").write_text("interactive\n", encoding="utf-8")

    counts = _scan_references(tmp_path, minimum_files=2)
    assert counts == {
        "harness": {"src/teatree/core/codex_adapter.py": 1},
        "attended": {"src/teatree/core/session.py": 1},
    }
    assert diff_pegs(counts["harness"], {}).over_peg == (("src/teatree/core/codex_adapter.py", 1, 0),)
    assert diff_pegs(counts["attended"], {"src/teatree/core/session.py": 2}).over_peg == ()


def test_failed_git_listing_raises(tmp_path: Path) -> None:
    with pytest.raises(CommandFailedError):
        _scan_references(tmp_path, minimum_files=1)


def test_implausibly_small_scan_raises(tmp_path: Path) -> None:
    run_checked(["git", "init", "-q", "-b", "main", str(tmp_path)])
    with pytest.raises(ValueError, match="scan read 0 files"):
        _scan_references(tmp_path, minimum_files=1)


def test_no_reference_grows_outside_its_home() -> None:
    counts = _scan_references(_ROOT, minimum_files=2000)
    for family in ("harness", "attended"):
        drift = diff_pegs(counts[family], load_pegs(family, toml_path=_PEGS))
        assert drift.over_peg == (), (
            f"{family}: move or reword new references outside their home: {drift.over_peg}. "
            "After a file rename, move its ledger entry to the new path without raising the count; "
            "after rebasing onto main, rebuild the ledger against that base."
        )
