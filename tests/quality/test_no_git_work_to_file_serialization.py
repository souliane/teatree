"""Fitness function: no module serializes git work to a patch/bundle file.

Backing up a git worktree or branch to a ``.patch`` / ``.bundle`` file (via
``git bundle`` or ``git format-patch``) is a banned anti-pattern in teatree. The
hand-off and the recovery snapshot are MARKDOWN-only — the durable-state text the
PreCompact hook writes (``teatree.core.handover``); unsynced or dirty worktrees
are KEPT by the #706 data-loss guard, never reaped onto a file; and salvage
captures unique content to a PR (push -> open PR -> verify -> delete the source),
never to a file. The dead ``bundle_create`` / ``bundle_create_at_sha`` leftovers
of the removed #1770 recovery-snapshot mechanism were excised; this gate keeps
that whole class from creeping back into the cleanup / recovery / hand-off /
git-utils surface.

The matcher is anti-vacuous (proven by the golden corpus below): it FLAGS a
re-added ``git bundle`` / ``git format-patch`` call or a ``.bundle`` / ``.patch``
file write, and does NOT flag the markdown snapshot, the salvage-to-PR path, a
``git diff`` captured into a variable, or the bare word "snapshot" in prose.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The cleanup / recovery / hand-off / git-utils modules that must never serialize
# git work to a file. The ``cleanup_*.py`` family is globbed so a new sibling is
# covered automatically.
_SCANNED_MODULES: tuple[Path, ...] = (
    _REPO_ROOT / "src" / "teatree" / "core" / "cleanup" / "cleanup.py",
    _REPO_ROOT / "src" / "teatree" / "core" / "handover.py",
    _REPO_ROOT / "src" / "teatree" / "core" / "stop_snapshot.py",
    _REPO_ROOT / "src" / "teatree" / "core" / "worktree" / "worktree_done.py",
    _REPO_ROOT / "src" / "teatree" / "utils" / "git_worktree.py",
    _REPO_ROOT / "src" / "teatree" / "utils" / "git.py",
    _REPO_ROOT / "hooks" / "scripts" / "hook_router.py",
    *sorted((_REPO_ROOT / "src" / "teatree" / "core" / "cleanup").glob("cleanup_*.py")),
)

# Each pattern matches a git-work-to-file SERIALIZATION call, not the benign word
# "snapshot" (the markdown hand-off) or "diff" (a ``git diff`` read into memory).
_SERIALIZATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # `git bundle ...` as a shell-string command.
    re.compile(r"\bgit\s+bundle\b"),
    # `bundle create` as adjacent tokens in a command string.
    re.compile(r"\bbundle\s+create\b"),
    # the args-list form `["bundle", "create", ...]` (how ``bundle_create`` was written).
    re.compile(r"""['"]bundle['"]\s*,\s*['"]create['"]"""),
    # `git format-patch` / `format_patch`.
    re.compile(r"format[-_]patch"),
    # writing a branch/worktree to a ``.bundle`` / ``.patch`` / ``.diff`` file.
    re.compile(r"""\.(?:bundle|patch|diff)['"]"""),
)


def _find_serialization(source: str) -> list[str]:
    """Return every git-work-to-file serialization snippet found in *source*."""
    return [m.group(0) for pattern in _SERIALIZATION_PATTERNS for m in pattern.finditer(source)]


@pytest.mark.parametrize("module", _SCANNED_MODULES, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_module_does_not_serialize_git_work_to_file(module: Path) -> None:
    """No scanned module contains a git-work-to-file serialization call."""
    assert module.is_file(), f"scanned module is missing (renamed?): {module.relative_to(_REPO_ROOT)}"
    hits = _find_serialization(module.read_text(encoding="utf-8"))
    assert not hits, (
        f"{module.relative_to(_REPO_ROOT)} serializes git work to a file ({hits!r}); "
        "backing a branch/worktree up to a patch/bundle file is banned — "
        "the hand-off/snapshot is markdown-only and salvage captures to a PR."
    )


# --- anti-vacuity corpus: prove the matcher is neither vacuous nor over-blocking ---

_MUST_FLAG: tuple[tuple[str, str], ...] = (
    ("bundle_create args-list", 'run_strict(repo=repo, args=["bundle", "create", bundle_path, branch])'),
    ("git bundle shell command", 'subprocess.run(["git", "bundle", "create", path, branch])'),
    ("format-patch", 'run(["git", "format-patch", "origin/main..HEAD", "-o", out_dir])'),
    ("format_patch underscore", 'cmd = "git format_patch HEAD~3"'),
    (".bundle file write", 'bundle_path = wt_dir / f"{branch}.bundle"'),
    (".patch file write", 'patch_file = Path(out) / "work.patch"'),
)

_MUST_NOT_FLAG: tuple[tuple[str, str], ...] = (
    ("markdown snapshot path", 'snapshot = _state_dir() / f"{prefix}{session_id}-precompact.md"'),
    ("snapshot read", 'text = snapshot.read_text(encoding="utf-8").strip()'),
    ("salvage branch ref", 'git.check(repo=repo, args=["branch", "-f", branch, request.source_ref])'),
    ("salvage to PR", "pr_url = hooks.open_pr(repo, branch, request.target)"),
    ("snapshot in prose", "# there is no recovery bundle or snapshot; nothing is serialized to a file"),
    ("git diff into memory", 'diff = git.run(repo=repo, args=["diff", f"{target}...{source_ref}"])'),
)


@pytest.mark.parametrize(("label", "snippet"), _MUST_FLAG, ids=[c[0] for c in _MUST_FLAG])
def test_matcher_flags_serialization(label: str, snippet: str) -> None:
    """The matcher catches a re-added bundle / format-patch / patch-file call."""
    assert _find_serialization(snippet), f"matcher missed a serialization call: {label}"


@pytest.mark.parametrize(("label", "snippet"), _MUST_NOT_FLAG, ids=[c[0] for c in _MUST_NOT_FLAG])
def test_matcher_ignores_legitimate_paths(label: str, snippet: str) -> None:
    """The matcher does not flag the markdown snapshot, salvage-to-PR, or git-diff reads."""
    assert not _find_serialization(snippet), f"matcher false-positived on a legitimate path: {label}"
