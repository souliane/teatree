"""Which checkouts exist, so a reaper can tell a dead env dir from a live one (#3852).

The single answer to "does a live checkout own this path?", shared by the two
reapers that ask it. Splitting that question in half is what made ``clean-all``
unsafe to run: the raw-orphan reaper asked git, while the isolated env-dir reaper
asked only the ``Worktree`` table — yet
:func:`teatree.paths.resolve_data_dir` mints an env dir for ANY checkout, whether
teatree registered it or not. The two ends of one deterministic slug mapping
therefore disagreed about the population, and every live-but-unregistered
checkout's control DB was reported as an orphan.

**The FILESYSTEM is the primary source, not a git registry.** The slug is
``sha256(checkout_path)``, so the question is literally "does this path exist on
disk" — and a registry-derived answer can only ever be a proxy for it. Asking
registries first made coverage depend on which clones happened to be
discoverable: on the host that produced this ticket, ``candidate_clones`` found
ONE clone (and only because it was the cwd) while 165 checkouts existed. A clone
that is never a candidate produces no gap at all, so its checkouts' absence from
the keep-set was indistinguishable from them being dead. Scanning for checkouts
directly removes that whole class: absence now means the path is not on disk.

:class:`CheckoutRegistry` carries the gaps alongside the answer on purpose. An
unreadable directory or an unreadable clone registry leaves an unknown number of
live checkouts unaccounted for, and an unaccounted checkout is indistinguishable
from a dead one — so a caller with a destructive disposition must fail CLOSED on
a non-empty ``gaps`` rather than delete on partial evidence (the #706 standard).

That only holds while ``complete`` means what it says, so **anything the walk
does not cover is a gap** (#3872). :data:`_NEVER_A_CHECKOUT` is the one
exclusion, and it is exempt because those dirs cannot hold a checkout the
resolver ever mints an env dir for. Everything else is walked, including
symlinked dirs; the depth cap reports rather than truncates.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.config import clone_root
from teatree.core.worktree.clone_paths import known_clone_paths
from teatree.core.worktree.worktree_roots import scanned_worktree_roots
from teatree.utils import git
from teatree.utils.run import CommandFailedError

#: A last-resort bound, NOT a coverage policy. Termination is already guaranteed
#: by the resolved-path dedup below, so this only has to sit clear of any real
#: tree: reaching it records a gap, and a gap keeps EVERY env dir — so a cap that
#: bites does not lose data, it silently costs the whole reclaim. Real depth grows
#: as nested agent worktrees accumulate (measured climbing past 15 within an hour
#: on the host, and symlink-following inflates it further), which is exactly why
#: the bound is set far above observed depth rather than tuned close to it.
_MAX_SCAN_DEPTH = 64

#: Skipped while scanning. Every entry is a directory that CANNOT be a teatree
#: checkout — a package/venv/tool cache. Nothing is skipped for being merely
#: large or unlikely: a skipped directory that did hold a checkout would be a
#: silent miss with no gap, which is the failure this scan exists to remove.
_NEVER_A_CHECKOUT = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


@dataclass(frozen=True, slots=True)
class CheckoutRegistry:
    """Every checkout path git reports across the known clones, plus what went unread."""

    paths: frozenset[str]
    gaps: tuple[str, ...]
    #: The roots this venue actually walked. A path outside every one of them was
    #: never looked for here, so its absence from :attr:`paths` says nothing about
    #: whether it exists — the distinction #3872 turns on (see
    #: :func:`~teatree.core.management.commands._workspace.owner_stamps.venue_can_observe`).
    scanned_roots: tuple[Path, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.gaps


def raw_worktree_paths(repo: str) -> dict[str, str]:
    """Return ``{worktree_path: branch}`` for every LINKED worktree of ``repo``.

    Parses ``git worktree list --porcelain``. The main checkout (the record whose
    path is ``repo`` itself) is excluded — only the linked worktrees are
    candidates. A detached worktree carries no ``branch`` line; it is recorded
    with the literal ``HEAD`` (:data:`git.DETACHED_HEAD`).

    Uses ``run_strict`` so a non-zero git exit RAISES. ``git.run`` passes
    ``expected_codes=None`` and therefore never raises: under it a corrupt clone
    returned an empty parse, every caller's ``except CommandFailedError`` was
    unreachable, and a failed registry read was indistinguishable from a clone
    with no worktrees. For the env-dir reaper that silence removed checkouts from
    the keep-set, turning a read failure into extra deletions — failing OPEN.
    """
    raw = git.run_strict(repo=repo, args=["worktree", "list", "--porcelain"])
    main = str(Path(repo).resolve())
    result: dict[str, str] = {}
    current_path = ""
    current_branch = ""
    for line in [*raw.splitlines(), "worktree "]:  # trailing sentinel flushes the last record
        if line.startswith("worktree "):
            if current_path and str(Path(current_path).resolve()) != main:
                result[current_path] = current_branch or git.DETACHED_HEAD
            current_path = line.removeprefix("worktree ")
            current_branch = ""
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
    return result


def candidate_clones(workspace: Path) -> set[str]:
    """The main clones whose worktree registries may hold orphaned worktrees.

    A worktree's registry lives in its source clone, so orphans are found by
    listing each known main clone's worktrees — the same clone set every other
    clone-wide sweep operates over (:func:`known_clone_paths`).
    """
    return {str(clone) for clone in known_clone_paths(workspace)}


def checkout_scan_roots(workspace: Path) -> tuple[Path, ...]:
    """The directory roots a checkout could live under, nested roots collapsed.

    The home directory is included unconditionally: teatree provisions worktrees
    under it, and a root list assembled only from configured paths is exactly the
    partial coverage that let an undiscoverable clone's checkouts read as dead.
    The configured roots are unioned on top so an operator who points teatree
    outside home is still covered.
    """
    candidates = {Path.home(), clone_root(), workspace, *scanned_worktree_roots(workspace)}
    resolved = {path.expanduser() for path in candidates}
    return tuple(
        sorted(root for root in resolved if not any(root != other and root.is_relative_to(other) for other in resolved))
    )


def _child_directories(directory: Path) -> tuple[list[Path], list[str]]:
    """The subdirectories of *directory* to walk, and what could not be read.

    A symlinked entry is an ordinary child here: ``is_dir`` follows it, so a
    checkout behind a link is walked like any other. Only :data:`_NEVER_A_CHECKOUT`
    is dropped without a gap.
    """
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        return [], [f"could not scan {directory} for checkouts ({exc})"]
    children: list[Path] = []
    gaps: list[str] = []
    for entry in entries:
        if entry.name in _NEVER_A_CHECKOUT:
            continue
        try:
            if entry.is_dir():
                children.append(entry)
        except OSError as exc:
            gaps.append(f"could not stat {entry} ({exc})")
    return children, gaps


def scan_checkout_paths(roots: tuple[Path, ...]) -> CheckoutRegistry:
    """Every directory under *roots* carrying a ``.git`` entry, plus what went unread.

    A checkout is any directory with a ``.git`` entry — a dir for a clone, a file
    for a linked worktree. The walk descends INTO checkouts, because teatree's
    agent worktrees nest inside their own clone (``<clone>/.claude/worktrees/…``),
    and THROUGH symlinked directories, because a symlinked dir is an ordinary way
    to reach a checkout — the host reaches its own teatree clone that way.

    **Every path the walk does not cover is a gap (#3872).** A skip that records
    nothing is worse than an unreadable one: it drops an unknown number of live
    checkouts from the keep-set while the answer still reads ``complete``, and
    that completeness is the whole evidence a deletion rests on. Skipping
    symlinks and truncating at the depth cap were both silent, and both were
    live on the host: hundreds of symlinked dirs and subtrees went unscanned
    with no gap recorded — among them the host's own teatree clone, reachable
    only through a symlink.

    Recursion is deduplicated on the resolved path, so a symlink loop terminates
    and a tree reachable by several spellings is walked once. The ``.git`` check
    runs BEFORE that dedup and before the listing, so a checkout is recorded
    under every spelling it is reached by, and one whose contents cannot be
    listed is still recorded rather than lost along with them.
    """
    found: set[str] = set()
    gaps: list[str] = []
    walked: set[str] = set()
    stack = [(root, 0) for root in roots]
    while stack:
        directory, depth = stack.pop()
        try:
            real = str(directory.resolve(strict=True))
        except OSError as exc:
            gaps.append(f"could not resolve {directory} ({exc})")
            continue
        try:
            carries_git = (directory / ".git").exists()
        except OSError as exc:
            # ``Path.exists`` re-raises EACCES rather than answering False.
            gaps.append(f"could not probe {directory} for a checkout ({exc})")
            continue
        if carries_git:
            found.add(str(directory))
            found.add(real)
        if real in walked:
            continue
        walked.add(real)
        if depth > _MAX_SCAN_DEPTH:
            gaps.append(f"stopped at depth {_MAX_SCAN_DEPTH} under {directory} — its subtree went unscanned")
            continue
        children, child_gaps = _child_directories(directory)
        gaps.extend(child_gaps)
        stack.extend((child, depth + 1) for child in children)
    return CheckoutRegistry(frozenset(found), tuple(gaps), roots)


def live_checkout_paths(workspace: Path) -> CheckoutRegistry:
    """Every checkout path that exists, from the filesystem UNION the git registries.

    The scan is the comprehensive source; the registries are additive, covering a
    checkout git knows about that sits outside every scanned root. Both contribute
    gaps, and a gap from either makes the whole answer incomplete — the sources
    widen coverage, they never vouch for each other.
    """
    scan = scan_checkout_paths(checkout_scan_roots(workspace))
    found = set(scan.paths)
    gaps = list(scan.gaps)
    for repo in sorted(candidate_clones(workspace)):
        try:
            worktrees = raw_worktree_paths(repo)
        except CommandFailedError as exc:
            gaps.append(f"clone {repo}: could not list worktrees ({exc})")
            continue
        found.add(repo)
        found.update(worktrees)
    return CheckoutRegistry(frozenset(found), tuple(gaps), scan.scanned_roots)


def one_spelling_each(paths: frozenset[str]) -> list[Path]:
    """*paths* with the spellings of one directory collapsed to a single entry.

    The scan records a checkout under every spelling it was reached by, which is
    right for a keep-set (a caller comparing strings must match either) and wrong
    for a work list: a reaper would visit the same directory twice and count it
    twice. An unresolvable path keeps its own spelling rather than dropping out.
    """
    seen: set[str] = set()
    unique: list[Path] = []
    for path in sorted(paths):
        try:
            key = str(Path(path).resolve())
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            unique.append(Path(path))
    return unique


def linked_worktree_paths(workspace: Path) -> CheckoutRegistry:
    """Every LINKED worktree that exists — the population a worktree GC may act on (#4244).

    The narrower sibling of :func:`live_checkout_paths`: main clones and the
    ad-hoc checkouts that are nobody's worktree are excluded, so a caller that
    removes what it is handed can never be handed a clone.

    Asking one directory for its worktrees is what made the pressure loop's GC
    inert for its whole life. It ran ``git worktree list`` against the worktree
    ROOT — a directory that CONTAINS worktrees and is not itself a repository —
    so git answered ``fatal: not a git repository``, the helper mapped that to
    ``[]``, and an unreadable answer became "nothing needs reaping" on every
    tick. Both halves were wrong: the enumeration cannot be run from a non-repo,
    and a worktree is registered by its source CLONE, not by whatever directory
    it happens to sit under.

    Two sources, unioned. The filesystem scan is primary (#3852): a checkout
    whose ``.git`` is a FILE is a linked worktree by construction, so it needs no
    registry to be found. Each scanned CLONE (``.git`` a directory) is then asked
    for its own registry, which reaches a worktree living outside every scanned
    root. A registry that will not answer is a gap, never an empty answer.
    """
    scan = scan_checkout_paths(checkout_scan_roots(workspace))
    found: set[str] = set()
    gaps = list(scan.gaps)
    for path in one_spelling_each(scan.paths):
        marker = path / ".git"
        try:
            registered_elsewhere = marker.is_file()
            is_clone = marker.is_dir()
        except OSError as exc:
            gaps.append(f"could not classify {path}'s .git entry ({exc})")
            continue
        if registered_elsewhere:
            found.add(str(path))
        elif is_clone:
            try:
                found.update(raw_worktree_paths(str(path)))
            except (CommandFailedError, OSError) as exc:
                gaps.append(f"clone {path}: could not list worktrees ({exc})")
    return CheckoutRegistry(frozenset(found), tuple(gaps), scan.scanned_roots)


__all__ = [
    "CheckoutRegistry",
    "candidate_clones",
    "checkout_scan_roots",
    "linked_worktree_paths",
    "live_checkout_paths",
    "one_spelling_each",
    "raw_worktree_paths",
    "scan_checkout_paths",
]
