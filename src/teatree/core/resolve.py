"""Worktree resolution from the user's original CWD.

Resolution order:

1. Walk up from CWD looking for the env cache copy → parse
    ``TICKET_DIR`` → match against ``Worktree.extra["worktree_path"]``
2. Match CWD directly against ``Worktree.extra["worktree_path"]``
3. Detect git worktree from filesystem and auto-register in DB

``T3_ORIG_CWD`` env var (set by the CLI) preserves the user's shell CWD
across the ``uv --directory`` subprocess chain.
"""

import logging
import os
import re
from pathlib import Path

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import CACHE_FILENAME
from teatree.utils import git

logger = logging.getLogger(__name__)

# Leading ``<number>`` of a ``<number>-<slug>`` branch (after an optional
# ``<scope>/``); digits buried later in the slug never match (see the parser).
_LEADING_TICKET_NUMBER = re.compile(r"^(?:[^/]*/)?(\d+)(?:-|$)")


class WorktreeNotFoundError(RuntimeError):
    """Raised when no worktree can be resolved from the current context."""


def _get_user_cwd() -> str:
    """Return the user's original CWD, surviving ``uv --directory`` and subprocess chains."""
    return os.environ.get("T3_ORIG_CWD", os.environ.get("PWD", str(Path.cwd())))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file (no shell expansion)."""
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _find_env_cache(cwd: str) -> Path | None:
    """Walk up from *cwd* looking for the env cache.

    The in-worktree env cache is a regular file copy (since #1316) of the
    canonical cache under ``.t3-cache/``; ``is_file()`` returns True for
    that copy and False for missing entries, so a worktree whose copy was
    never generated is naturally skipped.
    """
    cwd_path = Path(cwd)
    for parent in [cwd_path, *cwd_path.parents]:
        candidate = parent / CACHE_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _candidate_paths(path: str) -> list[str]:
    """Return de-duplicated list of path variants to try for DB lookups.

    On macOS, ``/var`` is a symlink to ``/private/var``, so a path stored
    as ``/var/folders/…`` won't match ``/private/var/folders/…`` (and vice
    versa).  We try the original, the resolved form, and the ``/private``
    prefix stripped/added variants.
    """
    candidates: list[str] = [path]
    resolved = str(Path(path).resolve())
    if resolved != path:
        candidates.append(resolved)
    # macOS: /private/var ↔ /var, /private/tmp ↔ /tmp, /private/etc ↔ /etc
    if path.startswith("/private/"):
        candidates.append(path.removeprefix("/private"))
    else:
        prefixed = "/private" + path
        if Path(prefixed).exists():
            candidates.append(prefixed)
    return candidates


def match_worktree_by_path(path: str) -> Worktree | None:
    """Find a Worktree whose ``extra["worktree_path"]`` matches or contains *path*.

    First tries an exact DB-level JSON lookup, then falls back to a prefix
    match for when the user is in a subdirectory of the worktree.
    Tries both the original and symlink-resolved path to handle macOS
    ``/var`` → ``/private/var`` differences.
    """
    # Fast path: exact match via DB-level JSON lookup
    for candidate in _candidate_paths(path):
        exact = Worktree.objects.filter(extra__worktree_path=candidate).first()
        if exact is not None:
            return exact

    # Walk up from path to find a parent that matches a stored worktree_path.
    # This handles being inside a subdirectory of a worktree.
    resolved_path = str(Path(path).resolve())
    path_obj = Path(resolved_path)
    for parent in path_obj.parents:
        for candidate in _candidate_paths(str(parent)):
            match = Worktree.objects.filter(extra__worktree_path=candidate).first()
            if match is not None:
                return match
        # Stop at filesystem root or home directory to avoid excessive queries
        parent_str = str(parent)
        if parent_str == str(Path.home()) or parent == parent.parent:
            break

    return None


def _ticket_number_from_branch(branch: str) -> str | None:
    """Return the ticket number a ``<number>-<slug>`` branch encodes, or None.

    Mirrors ``_workspace_ticket_intake.build_branch_name``: the number is the
    leading segment (optionally after a ``<scope>/`` prefix). Digits that appear
    later in the slug are not a ticket number and must not match.
    """
    match = _LEADING_TICKET_NUMBER.match(branch)
    return match.group(1) if match else None


def _ticket_by_number(number: str) -> Ticket | None:
    """Return the non-synthetic ticket whose ``ticket_number`` is *number*.

    ``ticket_number`` is a derived property (trailing digits of ``issue_url``,
    else the pk), so the match is done in Python over real tickets — synthetic
    ``auto:`` rows are excluded so a hint never resolves back to a placeholder.
    """
    for ticket in Ticket.objects.exclude(issue_url="").exclude(issue_url__startswith="auto:"):
        if ticket.ticket_number == number:
            return ticket
    return None


def _ticket_owning_branch(branch: str) -> Ticket | None:
    """Return the ticket whose ``ticket_number`` the branch encodes, or None.

    A manually-added worktree (``git worktree add`` without ``workspace
    ticket``) has no Worktree row yet, but its branch already names the
    ticket. Matching on that number attaches the worktree to the correct
    ticket instead of the most-recent workspace sibling.
    """
    number = _ticket_number_from_branch(branch)
    if number is None:
        return None
    return _ticket_by_number(number)


def _auto_register_from_git(cwd: str, ticket_hint: Ticket | None = None) -> Worktree | None:
    """Detect a git worktree from the filesystem and auto-register it in the DB.

    Reuses an existing Worktree row keyed by branch + repo before falling
    through to creating a new ``auto:<branch>`` ticket. This prevents duplicate
    ticket rows when a real-ticket worktree exists but its
    ``extra["worktree_path"]`` is missing or stale (which would make
    ``match_worktree_by_path`` miss it).

    Ticket attribution for a manually-added worktree resolves in order:
    an explicit *ticket_hint* (the ``--ticket`` flag), the branch-encoded
    ticket number (``_ticket_owning_branch``), the workspace-dir owner
    (``_workspace_owner_ticket``), then a fresh ``auto:<branch>`` ticket. The
    hint and branch number win first so a manual worktree never cross-attaches
    to an unrelated sibling under the same workspace dir.
    """
    cwd_path = Path(cwd).resolve()
    git_file = cwd_path / ".git"
    if not git_file.is_file():
        return None  # Not a git worktree (worktrees have .git as a file, not dir)

    branch = git.current_branch(repo=cwd)
    if not branch:
        return None

    repo_name = cwd_path.name
    existing = Worktree.objects.filter(branch=branch, repo_path=repo_name).first()
    if existing is not None:
        _refresh_reused_row(existing, branch, cwd_path)
        return existing

    ticket = (
        ticket_hint
        or _ticket_owning_branch(branch)
        or _workspace_owner_ticket(cwd_path)
        or Ticket.objects.get_or_create(
            issue_url=f"auto:{branch}",
            defaults={"variant": "", "repos": [repo_name]},
        )[0]
    )
    wt, wt_created = Worktree.objects.get_or_create(
        ticket=ticket,
        repo_path=repo_name,
        defaults={
            "overlay": ticket.overlay,
            "branch": branch,
            "extra": {"worktree_path": str(cwd_path)},
        },
    )
    if not wt_created:
        _refresh_reused_row(wt, branch, cwd_path)
    return wt


def _refresh_reused_row(worktree: Worktree, branch: str, cwd_path: Path) -> None:
    """Re-point a reused row at the git worktree actually being resolved.

    Both reuse arms above hand back an existing row whose recorded
    ``branch``/``extra.worktree_path`` may describe a PREVIOUS worktree of
    the same ticket+repo (the dir was re-created elsewhere, or the work
    moved to a new branch). ``get_or_create`` ignores its ``defaults`` on
    reuse, so without this refresh every downstream consumer (run
    commands, provision steps) keeps acting on the stale path — e.g. a
    frontend build silently lands in a different ticket's worktree
    directory while the user is sitting in the current one.
    """
    update_fields: list[str] = []
    if worktree.branch != branch:
        worktree.branch = branch
        update_fields.append("branch")
    extra = worktree.extra or {}
    if extra.get("worktree_path") != str(cwd_path):
        extra["worktree_path"] = str(cwd_path)
        worktree.extra = extra
        update_fields.append("extra")
    if update_fields:
        worktree.save(update_fields=update_fields)


def _workspace_owner_ticket(cwd_path: Path) -> Ticket | None:
    """Return the ticket that already owns *cwd_path*'s workspace dir, if any.

    A per-ticket workspace dir holds one repo worktree per affected repo
    (e.g. ``<workspace>/<ticket>/<repoA>``, ``…/<repoB>``). When a sibling
    worktree under the same parent directory is already registered, its
    ticket owns the workspace — a different branch/repo resolved from the
    same workspace must attach to that ticket rather than fork a fresh
    ``auto:<branch>`` ticket (#641).

    Stored ``worktree_path`` values are written unresolved (provision uses
    ``config.worktree_root()`` verbatim) while ``cwd_path`` here is
    ``.resolve()``-d, so a symlinked workspace root (macOS ``/tmp`` →
    ``/private/tmp``) would otherwise miss. Comparison goes through
    ``_candidate_paths`` — the same symlink-variant set
    ``match_worktree_by_path`` uses. Relies on the one-ticket-per-
    workspace-dir invariant; if violated the first match wins.
    """
    workspace_candidates = set(_candidate_paths(str(cwd_path.parent)))
    for wt in Worktree.objects.exclude(extra__worktree_path__isnull=True).order_by("pk"):
        recorded = (wt.extra or {}).get("worktree_path", "")
        if not recorded:
            continue
        if set(_candidate_paths(str(Path(recorded).parent))) & workspace_candidates:
            return wt.ticket
    return None


def _is_main_clone(path: str) -> bool:
    """Return True if *path* is a main git clone (not a worktree).

    Git worktrees have ``.git`` as a file pointing to the main repo's
    ``.git/worktrees/<name>`` directory. Main clones have ``.git`` as a
    directory.
    """
    git_marker = Path(path) / ".git"
    return git_marker.is_dir()


def _reject_main_clone(worktree: Worktree) -> None:
    """Raise ``WorktreeNotFoundError`` if *worktree* points at a main clone.

    Single source of truth for the main-clone refusal. Every
    ``resolve_worktree()`` return path that hands back a DB-matched
    ``Worktree`` must pass through this — a stale or mis-recorded env
    cache / row whose ``worktree_path`` is a main clone would otherwise
    route destructive consumers (db reset, teardown, cleanup) at the
    main clone instead of an isolated worktree (#752).
    """
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if wt_path and _is_main_clone(wt_path):
        msg = (
            f"Refusing to operate on main clone at {wt_path}.\n"
            "Create a worktree first: t3 <overlay> workspace ticket <issue_url>"
        )
        raise WorktreeNotFoundError(msg)


def _warn_cwd_mismatch(worktree: Worktree, cwd: str) -> None:
    """Log a warning when the resolved worktree path and user's CWD are unrelated.

    Either CWD should be inside the worktree path (running from a
    subdirectory), or the worktree path should be inside CWD (running
    from the ticket directory that contains the worktree).
    """
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if not wt_path:
        return
    cwd_resolved = str(Path(cwd).resolve())
    wt_resolved = str(Path(wt_path).resolve())
    if not cwd_resolved.startswith(wt_resolved) and not wt_resolved.startswith(cwd_resolved):
        logger.warning(
            "Resolved worktree path %s does not match CWD %s. You may be operating on the wrong worktree.",
            wt_resolved,
            cwd_resolved,
        )


def _rebind_ticket_if_synthetic(worktree: Worktree, ticket_hint: Ticket) -> None:
    """Re-attach *worktree* to *ticket_hint* when its current ticket is synthetic.

    An explicit ``--ticket`` correction only overrides auto-registration: a
    worktree already bound to a real ticket is left alone (the caller's hint
    cannot silently steal a correctly-attributed worktree), but one stuck on
    a placeholder ``auto:`` ticket is rebound to the named ticket.
    """
    current = worktree.ticket
    if current.pk == ticket_hint.pk:
        return
    if not current.issue_url.startswith("auto:"):
        return
    worktree.ticket = ticket_hint
    worktree.save(update_fields=["ticket"])


def _finalize_matched(worktree: Worktree, cwd: str, ticket_hint: Ticket | None) -> Worktree:
    """Apply the guards + optional ticket rebind shared by every DB-matched path."""
    _reject_main_clone(worktree)
    _warn_cwd_mismatch(worktree, cwd)
    if ticket_hint is not None:
        _rebind_ticket_if_synthetic(worktree, ticket_hint)
    return worktree


def resolve_worktree(path: str = "", ticket_hint: Ticket | None = None) -> Worktree:
    """Resolve a worktree from *path* or the user's CWD.

    Raises ``WorktreeNotFoundError`` if no worktree can be found or if
    the resolved path is a main repo clone (not a worktree).

    Logs a warning when the resolved worktree path doesn't contain the
    user's CWD, which may indicate the wrong worktree was matched.

    *ticket_hint* (the ``worktree provision --ticket`` flag) pins the ticket
    a newly-auto-registered worktree attaches to, and rebinds an already-
    resolved worktree that is stuck on a synthetic ``auto:`` ticket.
    """
    cwd = str(Path(path).resolve()) if path else _get_user_cwd()

    # 1. Walk up from CWD to find the env cache (a regular file copy of
    #    the canonical file in .t3-cache/, since #1316).
    envfile = _find_env_cache(cwd)
    if envfile is not None:
        env = _parse_env_file(envfile)
        ticket_dir = env.get("TICKET_DIR", "")
        if ticket_dir:
            wt = match_worktree_by_path(ticket_dir)
            if wt is not None:  # pragma: no branch
                return _finalize_matched(wt, cwd, ticket_hint)

    # 2. Match CWD directly against stored worktree paths
    wt = match_worktree_by_path(cwd)
    if wt is not None:
        return _finalize_matched(wt, cwd, ticket_hint)

    # 3. Detect git worktree from filesystem and auto-register
    wt = _auto_register_from_git(cwd, ticket_hint=ticket_hint)
    if wt is not None:
        return wt

    msg = f"Cannot auto-detect worktree from {cwd}.\nMake sure you are running t3 from inside a worktree directory."
    raise WorktreeNotFoundError(msg)
