"""Environmental resolution for the main-clone gate — the package-native twin.

The pure decision core :mod:`teatree.core.gates.main_clone_guard` classifies a
git command; it does NOT know which repo the command targets or whether that repo
is a teatree-managed main clone. Lane A's PreToolUse hook supplies those
environmental facts from ``hooks/scripts/managed_repo.py`` + its own
``_effective_command_dir`` / ``_is_managed_main_clone`` helpers — modules that
live OUTSIDE the importable ``teatree`` package so they stay import-safe in the
cold hook subprocess (no ``teatree`` on ``sys.path``).

Lane B (``pydantic_ai``) runs INSIDE the teatree process, so it cannot import the
cold-hook module (wrong direction, and the hook keeps its own copy for cold
safety). It needs the SAME facts computed from the SAME primitives. This module
is that package-native resolver: it consults the identical signals the cold-hook
toolkit does — the ``origin`` slug (:func:`teatree.hooks._repo_visibility.slug_for_cwd`),
the DB-home ``overlays`` registry (:func:`teatree.config.cold_reader.read_setting`),
the worktree-vs-clone marker (:func:`teatree.paths.running_from_worktree`), and
the ``-C``/``--git-dir`` resolver
(:func:`teatree.hooks._commit_repo_dir.resolve_commit_dir`) — so both lanes agree
on "is this a managed main clone" by construction. The git-command verdict itself
stays the SINGLE shared decision core (:func:`find_main_clone_git_mutation`); the
two lanes' agreement across BOTH the classifier and this environmental resolution
is pinned mechanically by the full-gate parity test.
"""

from pathlib import Path
from typing import Any, cast

from teatree.core.gates.main_clone_guard import deny_reason, find_main_clone_git_mutation
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

DEFAULT_PROTECTED_BRANCHES = frozenset({"main", "master"})


def _overlays_registry() -> dict[str, Any]:
    """The effective overlay registry: the DB-home ``overlays`` row (``{}`` when absent).

    Mirrors ``hooks/scripts/managed_repo.overlays_registry`` so both lanes read
    the identical managed-repo signals.
    """
    from teatree.config.cold_reader import read_setting  # noqa: PLC0415

    try:
        db = read_setting("overlays")
    except Exception:  # noqa: BLE001 — an unreadable/locked DB resolves to no registry.
        db = None
    return cast("dict[str, Any]", db) if isinstance(db, dict) else {}


def _managed_repo_signals() -> tuple[list[str], list[Path]]:
    """Return ``(repo_slug_substrings, overlay_base_paths)`` marking a repo managed.

    Teatree core's own slug is always included. Mirrors
    ``hooks/scripts/managed_repo.overlay_managed_repo_signals``.
    """
    slugs: list[str] = ["souliane/teatree"]
    paths: list[Path] = []
    for overlay_cfg in _overlays_registry().values():
        if not isinstance(overlay_cfg, dict):
            continue
        for key in ("workspace_repos", "frontend_repos", "public_repos"):
            slugs.extend(str(s).strip().lower() for s in overlay_cfg.get(key, []) if str(s).strip())
        base = overlay_cfg.get("path")
        if isinstance(base, str) and base.strip():
            try:
                paths.append(Path(base).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
    return slugs, paths


def repo_root_is_teatree_managed(repo_root: str) -> bool:
    """True iff *repo_root* is a teatree-MANAGED source repo (teatree core / an overlay repo).

    Returns ``False`` for any repo the managed-signal set does not cover and on
    any classification error (fail OPEN — an unmanaged clone must never block).
    """
    slugs, paths = _managed_repo_signals()
    try:
        root_resolved = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for base in paths:
        try:
            root_resolved.relative_to(base)
        except ValueError:
            continue
        return True
    try:
        from teatree.hooks._repo_visibility import slug_for_cwd  # noqa: PLC0415

        slug = slug_for_cwd(root_resolved).lower()
    except Exception:  # noqa: BLE001 — cannot resolve a slug → not provably managed.
        return False
    return any(entry in slug for entry in slugs) if slug else False


def is_managed_main_clone(repo_root: str) -> bool:
    """True iff *repo_root* is a REGISTERED (teatree-managed) primary clone.

    A linked worktree (``.git`` *file*) is NOT a main clone — work belongs there,
    so it allows — and only a ``.git``-*dir* primary clone that is teatree-managed
    qualifies. Any resolution error fails OPEN (``False``). Mirrors the hook's
    ``_is_managed_main_clone``.
    """
    try:
        root = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        from teatree.paths import running_from_worktree  # noqa: PLC0415

        if running_from_worktree(root):
            return False
    except Exception:  # noqa: BLE001 — cannot confirm worktree-vs-clone → fail OPEN.
        return False
    if not (root / ".git").is_dir():
        return False
    return repo_root_is_teatree_managed(str(root))


def effective_command_dir(command: str, cwd: "Path | None") -> "Path | None":
    """Resolve the dir whose repo a git command actually targets, else the cwd.

    Honours a leading ``cd``/``pushd`` and git's ``-C``/``--git-dir`` redirection
    via :func:`teatree.hooks._commit_repo_dir.resolve_commit_dir`, so the gate
    keys off the repo the command MUTATES, not the ambient cwd. Returns ``None``
    when the target cannot be pinned statically (a substitution marker) — failing
    OPEN rather than guessing a repo. Mirrors the hook's ``_effective_command_dir``.
    """
    from teatree.hooks._commit_repo_dir import resolve_commit_dir  # noqa: PLC0415

    try:
        resolved = resolve_commit_dir(command, cwd)
    except Exception:  # noqa: BLE001 — an unexpected resolver error falls back to cwd-keying.
        return cwd
    if not isinstance(resolved, Path):
        return None
    return resolved.parent if resolved.name == ".git" else resolved


def _resolve_repo_root(directory: str) -> str | None:
    """The working-tree root enclosing *directory*, or ``None`` (not a repo / git error)."""
    return _git_query(directory, "rev-parse", "--show-toplevel") or None


def _default_branch(repo: Path) -> str | None:
    """Resolve *repo*'s default branch (``origin/HEAD``, else the current branch), or ``None``."""
    head = _git_query(str(repo), "symbolic-ref", "refs/remotes/origin/HEAD")
    if head:
        return head.rsplit("/", 1)[-1]
    return _git_query(str(repo), "symbolic-ref", "--short", "HEAD") or None


def _load_protected_branches() -> frozenset[str]:
    """Defaults plus every overlay's declared ``protected_branches``."""
    branches = set(DEFAULT_PROTECTED_BRANCHES)
    for overlay_cfg in _overlays_registry().values():
        if isinstance(overlay_cfg, dict):
            branches.update(str(b) for b in overlay_cfg.get("protected_branches", []))
    return frozenset(branches)


def _git_query(directory: str, *args: str) -> str:
    """Run a read-only ``git`` query in *directory*; ``""`` on a non-zero exit/failure."""
    try:
        result = run_allowed_to_fail(
            ["git", "-C", directory, "--no-optional-locks", *args],
            expected_codes=None,
            timeout=3,
        )
    except (TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def main_clone_git_deny_reason(command: str, cwd: "Path | None") -> str | None:
    """Return the main-clone deny reason for *command* run from *cwd*, else ``None``.

    The full Lane-A-shaped gate: resolve the effective command dir (honouring
    ``-C``/``--git-dir``), find the enclosing repo root, and classify with the
    shared core ONLY when that root is a managed MAIN CLONE. A worktree cwd (the
    normal Lane-B dispatch), an unmanaged clone, or an unresolvable target all
    return ``None`` (allow), so the deny fires exactly when the command mutates a
    managed main clone — identical to the hook's ``_git_finding``.
    """
    effective = effective_command_dir(command, cwd)
    if effective is None:
        return None
    repo_root = _resolve_repo_root(str(effective))
    if repo_root is None or not is_managed_main_clone(repo_root):
        return None
    finding = find_main_clone_git_mutation(
        command,
        default_branch=_default_branch(Path(repo_root)),
        protected_branches=_load_protected_branches(),
    )
    return deny_reason(finding) if finding is not None else None
