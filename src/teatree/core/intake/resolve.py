"""Worktree resolution from the user's original CWD.

Resolution order:

1. Walk up from CWD to the worktree's out-of-repo ``.t3-cache/`` sibling
    holding the env cache → parse ``TICKET_DIR`` → match against
    ``Worktree.extra["worktree_path"]``
2. Match CWD directly against ``Worktree.extra["worktree_path"]``
3. Detect git worktree from filesystem and auto-register in DB

``T3_ORIG_CWD`` env var (set by the CLI) preserves the user's shell CWD
across the ``uv --directory`` subprocess chain.
"""

import logging
import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.core.intake.ticket_kind_classification import classify_ticket_kind
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_all_overlays, get_overlay_for_repo
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from teatree.core.worktree.worktree_paths import _candidate_paths
from teatree.utils import git

logger = logging.getLogger(__name__)

# Leading ``<number>`` of a ``<number>-<slug>`` branch (after an optional
# ``<scope>/``); digits buried later in the slug never match (see the parser).
_LEADING_TICKET_NUMBER = re.compile(r"^(?:[^/]*/)?(\d+)(?:-|$)")


class WorktreeNotFoundError(RuntimeError):
    """Raised when no worktree can be resolved from the current context."""


class TicketIdentityCollisionError(RuntimeError):
    """Raised when a derived ``ticket_number`` resolves to more than one ticket.

    ``ticket_number`` is a DERIVED, non-unique key (trailing digits of
    ``issue_url``, else the pk), so two tickets on different repos/forges can
    share one. Returning an arbitrary ``.first()`` silently cross-attaches a
    worktree to the wrong ticket; failing loud surfaces the ambiguity instead.
    """


class WorktreePathConflictError(RuntimeError):
    """Raised when refreshing a row would steal a ``worktree_path`` another row owns.

    Repointing row A onto a path row B already records would leave two rows
    claiming one directory — every downstream consumer (run, provision,
    teardown) would then disagree about which ticket owns it. Fail loud rather
    than silently repoint.
    """


class WorkspaceOwnerCollisionError(RuntimeError):
    """Raised when one workspace dir is owned by worktrees of more than one ticket.

    The one-ticket-per-workspace-dir invariant is violated. Picking an
    arbitrary owner mis-attributes a sibling worktree; failing loud tells the
    operator to run the command from a specific worktree subdir.
    """


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
    """Walk up from *cwd* looking for the repo's env cache.

    The env cache lives at ``<ticket_dir>/.t3-cache/<repo>/.t3-env.cache`` —
    under the out-of-repo ``.t3-cache/`` sibling of the repo working tree,
    keyed per repo so sibling repos of one ticket do not share one file
    (souliane/teatree#3097). Each walked directory is treated as a candidate
    repo worktree dir: its cache is ``<parent>/.t3-cache/<dir-name>/.t3-env.cache``.
    Walking up from inside a repo reaches the repo dir and finds its own cache;
    a sibling repo's cache is never returned. ``is_file()`` is False for a
    worktree whose cache was never generated, so it is naturally skipped.
    """
    cwd_path = Path(cwd)
    for parent in [cwd_path, *cwd_path.parents]:
        candidate = parent.parent / CACHE_DIRNAME / parent.name / CACHE_FILENAME
        if candidate.is_file():
            return candidate
    return None


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

    Mirrors ``_workspace.ticket_intake.build_branch_name``: the number is the
    leading segment (optionally after a ``<scope>/`` prefix). Digits that appear
    later in the slug are not a ticket number and must not match.
    """
    match = _LEADING_TICKET_NUMBER.match(branch)
    return match.group(1) if match else None


def _ticket_by_number(number: str, *, overlay: str | None = None) -> Ticket | None:
    """Return the non-synthetic ticket whose ``ticket_number`` is *number*.

    ``ticket_number`` is a DERIVED, non-unique key (trailing digits of
    ``issue_url``, else the pk), so two tickets on different repos/forges can
    share one. The forge-number branch is served by the indexed ``issue_number``
    column (denormalized on ``save``); the pk-fallback branch — a real ticket
    whose ``issue_url`` carries no forge number, so ``ticket_number`` degrades to
    ``str(pk)`` — is matched by pk. Both exclude synthetic ``auto:`` rows so a
    hint never resolves back to a placeholder. Together they reproduce the old
    O(all tickets) Python scan as an indexed lookup.

    When *overlay* is known (inferred from the resolving repo's remote) the
    candidates are narrowed to that overlay first, so a same-number ticket in a
    different overlay never competes. More than one surviving candidate raises
    :class:`TicketIdentityCollisionError` (fail loud) rather than returning an
    arbitrary ``.first()`` that would cross-attach the worktree to the wrong
    ticket.
    """
    real = Ticket.objects.exclude(issue_url="").exclude(issue_url__startswith="auto:")
    matches = list(real.filter(issue_number=number))
    if number.isdigit():
        # ``issue_number`` is blank exactly when ``ticket_number`` falls back to
        # ``str(pk)``, so the pk lookup is the fallback branch (never double-counts
        # a row already matched above, whose ``issue_number`` is non-blank).
        matches += list(real.filter(issue_number="", pk=int(number)))
    if overlay:
        scoped = [ticket for ticket in matches if ticket.overlay == overlay]
        if scoped:
            matches = scoped
    if len(matches) > 1:
        msg = (
            f"ticket_number {number!r} resolves to {len(matches)} tickets "
            f"(pks {sorted(ticket.pk for ticket in matches)}); refusing to attach a "
            "worktree to an arbitrary one. Pass --ticket to disambiguate."
        )
        raise TicketIdentityCollisionError(msg)
    return matches[0] if matches else None


def _ticket_owning_branch(branch: str, *, overlay: str | None = None) -> Ticket | None:
    """Return the ticket whose ``ticket_number`` the branch encodes, or None.

    A manually-added worktree (``git worktree add`` without ``workspace
    ticket``) has no Worktree row yet, but its branch already names the
    ticket. Matching on that number attaches the worktree to the correct
    ticket instead of the most-recent workspace sibling. *overlay* scopes the
    number lookup (see :func:`_ticket_by_number`).
    """
    number = _ticket_number_from_branch(branch)
    if number is None:
        return None
    return _ticket_by_number(number, overlay=overlay)


def _overlay_name_for_cwd(cwd_path: Path) -> str | None:
    """Best-effort overlay name owning the repo at *cwd_path* (or ``None``).

    Scopes ``_ticket_by_number`` so a same-number ticket in another overlay
    never collides. Resolution is via the repo's ``origin`` remote
    (:func:`get_overlay_for_repo`); a repo with no recognised remote yields
    ``None`` and the number match stays global (still fail-loud on >1).
    """
    try:
        overlay = get_overlay_for_repo(str(cwd_path))
    except ImproperlyConfigured:
        return None
    if overlay is None:
        return None
    for name, candidate in get_all_overlays().items():
        if candidate is overlay:
            return name
    return None


def _auto_register_from_git(cwd: str, ticket_hint: Ticket | None = None) -> Worktree | None:
    """Detect a git worktree from the filesystem and auto-register it in the DB.

    Ticket attribution for a manually-added worktree resolves in order:
    an explicit *ticket_hint* (the ``--ticket`` flag), the branch-encoded
    ticket number (``_ticket_owning_branch``, overlay-scoped), then the
    workspace-dir owner (``_workspace_owner_ticket``). The attribution chain
    runs FIRST so a real signal always wins over a foreign row that merely
    shares this branch + repo basename — the cross-attach that bound a worktree
    to a merged ticket.

    Only when the chain yields nothing does the legacy branch+repo reuse fire,
    as a LAST resort before forking a fresh ``auto:<branch>`` ticket — it
    rescues a moved or stale-``worktree_path`` worktree of a non-numbered branch
    without forking a duplicate, and can no longer out-vote real attribution.
    """
    cwd_path = Path(cwd).resolve()
    git_file = cwd_path / ".git"
    if not git_file.is_file():
        return None  # Not a git worktree (worktrees have .git as a file, not dir)

    branch = git.current_branch(repo=cwd)
    if not branch:
        return None

    repo_name = cwd_path.name
    overlay_name = _overlay_name_for_cwd(cwd_path)
    ticket = ticket_hint or _ticket_owning_branch(branch, overlay=overlay_name) or _workspace_owner_ticket(cwd_path)
    if ticket is not None:
        return _get_or_refresh_worktree(ticket, repo_name, branch, cwd_path)

    existing = Worktree.objects.filter(branch=branch, repo_path=repo_name).first()
    if existing is not None:
        _refresh_reused_row(existing, branch, cwd_path)
        return existing

    ticket = Ticket.objects.get_or_create(
        issue_url=f"auto:{branch}",
        defaults={"variant": "", "repos": [repo_name], "kind": classify_ticket_kind(title=branch)},
    )[0]
    return _get_or_refresh_worktree(ticket, repo_name, branch, cwd_path)


def _get_or_refresh_worktree(ticket: Ticket, repo_name: str, branch: str, cwd_path: Path) -> Worktree:
    """Reuse *ticket*'s row for *repo_name* (mirror ``provision.py``) or create it.

    The reuse is scoped to the RESOLVED ticket, so a foreign ticket's row that
    happens to share this branch + repo basename is never stolen.
    """
    wt, created = Worktree.objects.get_or_create(
        ticket=ticket,
        repo_path=repo_name,
        defaults={
            "overlay": ticket.overlay,
            "branch": branch,
            "extra": {"worktree_path": str(cwd_path)},
        },
    )
    if not created:
        _refresh_reused_row(wt, branch, cwd_path)
    return wt


def _refresh_reused_row(worktree: Worktree, branch: str, cwd_path: Path) -> None:
    """Re-point a reused row at the git worktree actually being resolved.

    A reused row's recorded ``branch``/``extra.worktree_path`` may describe a
    PREVIOUS worktree of the same ticket+repo (the dir was re-created
    elsewhere, or the work moved to a new branch). ``get_or_create`` ignores
    its ``defaults`` on reuse, so without this refresh every downstream
    consumer (run commands, provision steps) keeps acting on the stale path.

    Refuses to repoint onto a ``worktree_path`` another row already owns: two
    rows claiming one directory is the collision this fix forecloses, so it
    raises :class:`WorktreePathConflictError` rather than silently steal.
    """
    update_fields: list[str] = []
    if worktree.branch != branch:
        worktree.branch = branch
        update_fields.append("branch")
    extra = worktree.extra or {}
    new_path = str(cwd_path)
    if extra.get("worktree_path") != new_path:
        _assert_path_unclaimed(worktree, new_path)
        extra["worktree_path"] = new_path
        worktree.extra = extra
        update_fields.append("extra")
    if update_fields:
        worktree.save(update_fields=update_fields)


def _assert_path_unclaimed(worktree: Worktree, new_path: str) -> None:
    """Raise if a row other than *worktree* already records *new_path*."""
    conflict = (
        Worktree.objects.exclude(pk=worktree.pk).filter(extra__worktree_path__in=_candidate_paths(new_path)).first()
    )
    if conflict is not None:
        msg = (
            f"Refusing to repoint worktree #{worktree.pk} onto {new_path}: "
            f"worktree #{conflict.pk} (ticket {conflict.ticket_id}) already owns it. "
            "Two rows must never claim one directory."
        )
        raise WorktreePathConflictError(msg)


def tickets_owning_workspace_dir(workspace_dir: Path) -> list[Ticket]:
    """Return the distinct tickets whose worktrees live directly under *workspace_dir*.

    A per-ticket workspace dir holds one repo worktree per affected repo
    (``<workspace>/<ticket>/<repoA>``, ``…/<repoB>``). A worktree belongs to
    *workspace_dir* when its stored ``worktree_path``'s parent matches it.

    Stored ``worktree_path`` values are written unresolved (provision uses
    ``config.worktree_root()`` verbatim) while callers pass a ``.resolve()``-d
    dir, so comparison goes through ``_candidate_paths`` — the same
    symlink-variant set ``match_worktree_by_path`` uses (macOS ``/tmp`` →
    ``/private/tmp``). This is the single source of truth for workspace-dir →
    ticket attribution, routed through by both the auto-register attribution
    chain and the ``workspace`` command resolver.
    """
    workspace_candidates = set(_candidate_paths(str(workspace_dir)))
    owners: dict[int, Ticket] = {}
    for wt in Worktree.objects.exclude(extra__worktree_path__isnull=True).order_by("pk"):
        recorded = (wt.extra or {}).get("worktree_path", "")
        if not recorded:
            continue
        if set(_candidate_paths(str(Path(recorded).parent))) & workspace_candidates:
            owners.setdefault(wt.ticket_id, wt.ticket)
    return list(owners.values())


def workspace_owner_ticket(workspace_dir: Path) -> Ticket | None:
    """Return the single ticket owning *workspace_dir*, or ``None``; fail loud on >1.

    The one-ticket-per-workspace-dir invariant: at most one ticket's worktrees
    share a workspace dir (#641). More than one owner means the invariant is
    violated; raising :class:`WorkspaceOwnerCollisionError` is the single
    fail-loud policy both ``_auto_register_from_git`` and the ``workspace``
    command resolver share — never an arbitrary pick.
    """
    owners = tickets_owning_workspace_dir(workspace_dir)
    if len(owners) > 1:
        msg = (
            f"{workspace_dir} holds worktrees from {len(owners)} tickets "
            f"(pks {sorted(ticket.pk for ticket in owners)}). Run the command "
            "from a specific worktree subdir."
        )
        raise WorkspaceOwnerCollisionError(msg)
    return owners[0] if owners else None


def _workspace_owner_ticket(cwd_path: Path) -> Ticket | None:
    """Return the ticket that owns *cwd_path*'s workspace dir, if any.

    Thin wrapper resolving the workspace dir (the parent that holds the repo
    subdirs) and delegating to :func:`workspace_owner_ticket` so the
    attribution chain shares the one fail-loud multi-owner policy (#641).
    """
    return workspace_owner_ticket(cwd_path.parent)


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

    # 1. Walk up from CWD to the .t3-cache/ sibling holding the env cache
    #    (out-of-repo, never copied into a repo tree, since #3097).
    envfile = _find_env_cache(cwd)
    if envfile is not None:
        env = _parse_env_file(envfile)
        ticket_dir = env.get("TICKET_DIR", "")
        if ticket_dir:
            wt = match_worktree_by_path(ticket_dir)
            if wt is not None:
                return _finalize_matched(wt, cwd, ticket_hint)
            # A stale env cache can name a TICKET_DIR whose worktree row is gone
            # (removed / never registered); fall through to the CWD-direct and
            # git-auto-register paths rather than treating the cache as truth.

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
