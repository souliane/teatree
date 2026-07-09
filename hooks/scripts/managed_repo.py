"""Managed-repo / worktree-vs-main-clone path toolkit shared across gates.

The worktree-first gates (``handle_protect_default_branch``, the out-of-band
merge gate, and the main-clone mutation gate ``main_clone_guard``) all answer
the same three questions about a path: which git repo encloses it, whether that
repo is a teatree-MANAGED source repo, and whether a path is harness state
rather than repo source. Keeping one copy here means the protected-branch gate
and the main-clone gate cannot drift on what counts as managed.

``hook_router`` re-imports these under their original ``_`` names, so its
existing call sites are unchanged; new siblings import the public names
directly instead of reaching back into the router (which would cycle —
``hook_router`` imports this module at top level).
"""

import contextlib
import re
import subprocess  # noqa: S404 — hook code legitimately shells `git` (mirrors hook_router).
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

# Alias the bare and ``hooks.scripts.`` identities to ONE module object so the
# router's ``from managed_repo import ...`` and a test's
# ``import hooks.scripts.managed_repo`` resolve the same globals — the pattern
# every sibling (``config_overwrite_guard``, ``unknown_repo_push_gate``) uses.
sys.modules.setdefault("managed_repo", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.managed_repo", sys.modules[__name__])

DEFAULT_PROTECTED_BRANCHES = {"main", "master"}

# Agent-harness state dirs that may sit UNDER a git repo's working tree
# (e.g. ``~/.claude`` inside a dotfiles repo) but whose files are never
# repo source. A Write here must never be blocked by the protected-branch
# gate — editing agent memory / todos / per-project state on `main` is
# exactly what the agent is supposed to do. Mirrors ``_KEEP_PATTERNS``.
_AGENT_STATE_PATH_RE = re.compile(
    r"/\.(claude|codex|cursor|copilot)/(projects/.*/memory/|memory/|todos/|statsig/|.*\.log$)",
)


@contextlib.contextmanager
def teatree_src_on_path() -> Iterator[None]:
    """Put the sibling ``src/`` on ``sys.path`` for the block, then restore it.

    The hook runs in the user's session shell with no guarantee ``teatree`` is
    importable (#1314); this is the shared bootstrap the lazy ``teatree.hooks``
    imports in the managed-repo gates rely on.
    """
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    added = src_dir not in sys.path
    if added:
        sys.path.insert(0, src_dir)
    try:
        yield
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(src_dir)


def db_overlays_registry() -> dict[str, Any] | None:
    """The DB-home ``overlays`` registry dict, or ``None`` on any absence/failure.

    The cold-hook twin of ``loader._inject_db_registries``: reads the canonical
    ``ConfigSetting`` ``overlays`` row Django-free via ``cold_reader``, through the
    same :func:`teatree_src_on_path` bootstrap the managed-repo gates use to reach
    ``teatree.*``. Fails open to ``None`` on ANY error — ``teatree`` unimportable,
    an unreadable/locked DB, a missing row, a non-dict value — so the caller resolves
    to an empty registry (never-lockout).
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import read_setting  # noqa: PLC0415

            value = read_setting("overlays")
    except Exception:  # noqa: BLE001
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def overlays_registry() -> dict[str, Any]:
    """The effective overlay registry: the DB-home ``overlays`` ``ConfigSetting`` row.

    Reads the migrated ``overlays`` row DB-only via :func:`db_overlays_registry`.
    ``{}`` when the DB read yields nothing/empty or fails.
    """
    return db_overlays_registry() or {}


def load_protected_branches() -> set[str]:
    """Return the merged set of protected branches from defaults + all overlays.

    The overlay registry resolves DB-only via :func:`overlays_registry`, so it
    protects an overlay's declared ``development`` / ``release`` branches instead of
    degrading to ``{main, master}`` only.
    """
    branches = set(DEFAULT_PROTECTED_BRANCHES)
    for overlay_cfg in overlays_registry().values():
        if isinstance(overlay_cfg, dict):
            branches.update(overlay_cfg.get("protected_branches", []))
    return branches


def is_agent_state_path(file_path: str) -> bool:
    """True iff *file_path* is agent-harness state, not repo source.

    Resolved to an absolute, symlink-free path first so a relative or
    ``..``-laden path can't dodge the pattern. A resolution failure (a
    path under a missing dir) falls back to the raw string — the regex
    is anchored on the harness-dir segment, which survives either form.
    """
    try:
        resolved = str(Path(file_path).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = file_path
    return _AGENT_STATE_PATH_RE.search(resolved) is not None


def file_is_inside_worktree(repo_root: str, file_path: str) -> bool:
    """True iff *file_path* resolves to a path inside *repo_root*'s working tree.

    ``git -C <parent> rev-parse`` walks UP to the nearest enclosing
    ``.git``, so the resolved repo root can be an ANCESTOR of the file
    (a dotfiles/home repo the file merely sits under). Confirming the
    file is genuinely within that root is what scopes the gate to the
    TARGET FILE's repo rather than whatever happens to enclose its parent
    dir (#126). A resolution failure means we cannot confirm containment —
    fail open (return ``False``, do not block).
    """
    try:
        file_resolved = Path(file_path).expanduser().resolve()
        root_resolved = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        file_resolved.relative_to(root_resolved)
    except ValueError:
        return False
    return True


def overlay_managed_repo_signals() -> tuple[list[str], list[Path]]:
    """Return ``(repo_slug_substrings, overlay_base_paths)`` from the overlay registry.

    Collects the two signals that mark a repo teatree-managed: the per-overlay repo
    slug lists (``workspace_repos`` / ``frontend_repos`` / ``public_repos``) and
    each overlay's ``path`` working-tree base. Teatree core's own slug
    (``souliane/teatree``) is always included. The registry resolves DB-only via
    :func:`overlays_registry`, so it recognises an overlay's product repos as
    managed. Fails to the core-only signal set when the DB read yields nothing — the
    caller treats "no resolvable signal + a resolvable slug" as unmanaged, never as a
    license to weaken the gate on uncertainty.
    """
    slugs: list[str] = ["souliane/teatree"]
    paths: list[Path] = []
    for overlay_cfg in overlays_registry().values():
        if not isinstance(overlay_cfg, dict):
            continue
        for key in ("workspace_repos", "frontend_repos", "public_repos"):
            slugs.extend(str(s).strip().lower() for s in overlay_cfg.get(key, []) if str(s).strip())
        base = overlay_cfg.get("path")
        if isinstance(base, str) and base.strip():
            with contextlib.suppress(OSError, RuntimeError):
                paths.append(Path(base).expanduser().resolve())
    return slugs, paths


def repo_root_is_teatree_managed(repo_root: str) -> bool:
    """True iff *repo_root* is a teatree-MANAGED source repo.

    The worktree-first gates guard only teatree core + the active overlay's
    registered repos (the overlay registry's ``workspace_repos`` /
    ``frontend_repos`` / ``public_repos`` slugs, plus each overlay ``path``;
    DB-home) — NOT every git repo (#126). An
    unmanaged repo (a dotfiles repo, an unrelated
    clone) must not block, so this returns ``False`` for any repo the
    managed-signal set does not cover, and ``False`` on any classification
    error (fail OPEN — the gate-over-deny class this whole change closes).

    Reuses :func:`overlay_managed_repo_signals` (the same signal source as the
    out-of-band-merge gate) and ``publish_surface.slug_for_cwd`` so the slug
    shape matches the rest of the managed-repo machinery.
    """
    slugs, paths = overlay_managed_repo_signals()
    try:
        root_resolved = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for base in paths:
        # ``Path.relative_to`` raises ``ValueError`` (NOT OSError/RuntimeError)
        # when ``root_resolved`` is not under ``base`` — the normal "this repo is
        # not under that managed base" case when several overlays register bases.
        # It must be suppressed so a non-matching base is skipped and the next
        # base is tried, instead of crashing the whole managed-repo classifier
        # (which fails the caller open, silently disabling the main-clone guard).
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            root_resolved.relative_to(base)
            return True
    try:
        with teatree_src_on_path():
            from teatree.hooks import publish_surface  # noqa: PLC0415

            slug = publish_surface.slug_for_cwd(root_resolved).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(entry in slug for entry in slugs) if slug else False


def resolve_branch_and_root(parent: str) -> tuple[str, str] | None:
    """Return ``(branch, repo_root)`` for the repo enclosing *parent*, or ``None``.

    ``None`` when *parent* is not inside a git repo, on a git error, or on
    a timeout — every one of which fails the gate open. ``git -C`` walks UP
    to the nearest ``.git``, so the returned root can be an ancestor of the
    file; :func:`file_is_inside_worktree` is what re-scopes it.
    """

    def _rev_parse(*flags: str) -> str:
        return subprocess.check_output(  # noqa: S603
            ["git", "-C", parent, "--no-optional-locks", "rev-parse", *flags],  # noqa: S607
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        return _rev_parse("--abbrev-ref", "HEAD"), _rev_parse("--show-toplevel")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def default_branch(repo: Path) -> str | None:
    """Resolve *repo*'s default branch (e.g. ``main`` / ``develop``), else None.

    Primary signal: ``origin/HEAD`` (the remote's default branch pointer). When
    that pointer is UNSET — a clone created without ``git remote set-head``, or
    one whose true default is ``develop``/``trunk`` and was never recorded —
    fall back to the branch the clone is currently ON. A correctly-maintained
    main clone sits on its own default branch, so its current branch is the best
    remaining default signal; without this fallback the gate would over-block
    ``git checkout develop`` (and ``t3 update``'s checkout) on such a clone,
    since ``develop`` is neither ``origin/HEAD``-resolvable nor in the static
    ``{main, master}`` protected set. The only branch the fallback ever adds to
    the safe set is the one the clone is already on, so checking it out is a
    no-op — it can never widen the gate to allow a NEW off-default switch.

    A DETACHED HEAD has no symbolic branch name, so the fallback yields ``None``
    (``HEAD`` is never a useful checkout target). Returns ``None`` only when
    neither signal resolves — the gate then falls back to the protected set.
    """
    head = _git_text(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head:
        return head.rsplit("/", 1)[-1]
    return _git_text(repo, "symbolic-ref", "--short", "HEAD") or None


def _git_text(repo: Path, *args: str) -> str:
    """Run a read-only ``git`` query in *repo*; ``""`` on any failure/timeout."""
    try:
        return subprocess.check_output(  # noqa: S603
            ["git", "-C", str(repo), "--no-optional-locks", *args],  # noqa: S607
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
