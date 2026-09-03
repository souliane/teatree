"""Decision core for the single-branch-repo gate — one branch, one PR, no second worktree.

Some repos are, for a stretch of their life, deliberately ONE branch wide. A fork
bootstrap is the standing example: everything lands on one long-lived integration
branch behind one open PR until that PR merges, because the repo has no reviewed
history yet to base a second branch on, so any sibling request would have to be
re-based onto whatever the open one becomes.

Prose did not hold this. The recorded outcome on the two repos that carry the rule
was 31 worktrees across 37 local branches, two of them duplicating a fix already
briefed onto the integration branch — so the same work was done twice while the
rule was written down three times. This module is the mechanism that replaces the
prose: while a repo is declared single-branch, creating a SECOND branch or
worktree in it is refused at the seam that would create it.

The declaration is config, not code — ``single_branch_repos``, a list of
``<repo-slug>=<branch>`` entries. That is the cleanest seam available: it is
overlay-scoped (each overlay names its own repos), it needs no forge round-trip on
a provisioning hot path, and REMOVING the entry is what ends the rule — a one-line
config change that is itself the dated record of the integration PR having merged.
Deriving the pinned branch from "the repo's single open PR" was the alternative
and was not taken: it makes every worktree creation depend on a forge call that
can be slow, rate-limited, or offline, and neither failure direction is
acceptable (fail-open silently drops the rule, fail-closed wedges provisioning).

Two consumers, one decision:

* :func:`check_branch_admitted` — the ``t3 <overlay> worktree provision`` path,
    which knows the repo and the branch it is about to create as data.
* :func:`find_second_branch_creation` — the Bash PreToolUse path, which has only
    a command string, and must catch the raw ``git worktree add`` /
    ``git checkout -b`` / ``git push`` that bypasses ``t3`` entirely.

The blocked set is precise, so ordinary work on the pinned branch is never
touched: creating a worktree, creating a branch, or pushing a branch OTHER than
the pinned one. Committing, fetching, pulling, checking the pinned branch out,
pushing the pinned branch, and every read-only git command allow.
"""

import shlex
from dataclasses import dataclass

#: The kill-switch key. A COLD-HOOK setting rather than a ``UserSettings`` field
#: because the Bash seam reads it from a hook with no importable teatree; the
#: provisioner reads the SAME key through ``cold_reader`` so one flip governs both.
GATE_KEY = "single_branch_repo_gate_enabled"

# Compound-command separators. A blocked verb hiding after ``&&``/``;`` in an
# otherwise-benign chain (``cd x && git checkout -b feature``) still creates a
# second branch, so each segment is classified independently. Mirrors
# :mod:`teatree.core.gates.main_clone_guard`, which learned the same lesson.
_SEGMENT_SEPARATORS = ("&&", "||", ";", "|", "&", "\n")

# git's leading global options that consume the NEXT token as their value, so the
# subcommand scanner skips two tokens for them (``git -C <path> worktree add``).
_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)

# ``git push <remote> <refspec>…`` — fewer positionals than this names no refspec,
# so the destination is the checked-out branch rather than an argument.
_PUSH_POSITIONALS_WITH_REFSPEC = 2

_CHECKOUT_CREATE_FLAGS = frozenset({"-b", "-B", "--orphan"})
_SWITCH_CREATE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create", "--orphan"})

# ``git branch`` invocations that inspect or delete rather than create. A delete is
# how a stale branch is CLEANED UP, so refusing it would make the gate fight the
# very sweep that brings a repo back into compliance.
_BRANCH_NON_CREATING_FLAGS = frozenset(
    {
        "-d",
        "-D",
        "--delete",
        "-l",
        "--list",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "-m",
        "-M",
        "--move",
        "--merged",
        "--no-merged",
        "--contains",
        "--points-at",
        "--show-current",
        "--set-upstream-to",
        "-u",
        "--unset-upstream",
        "--edit-description",
    }
)

#: What a finding's ``surface`` can be — which creation seam tripped.
type CreationSurface = str


@dataclass(frozen=True, slots=True)
class SingleBranchFinding:
    """A second-branch/worktree creation the gate refuses.

    ``surface`` names the seam (``"worktree"``, ``"branch"``, ``"push"``,
    ``"provision"``); ``target`` is the branch (or rendered invocation) the deny
    message names, so the operator sees WHAT was refused, not just that something
    was.
    """

    surface: CreationSurface
    target: str


def parse_single_branch_repos(entries: list[str]) -> dict[str, str]:
    """``["<slug>=<branch>", …]`` as ``{slug: branch}``, skipping malformed entries.

    A malformed entry is DROPPED rather than raising: this is read on a
    provisioning path and on a cold hook path, and a typo in one entry must not
    take out provisioning for every other repo. The gate then simply does not
    apply to that slug, which is the same behaviour as not declaring it — visible
    as the rule not firing, rather than as a traceback nobody reads.
    """
    parsed: dict[str, str] = {}
    for entry in entries:
        slug, _, branch = entry.partition("=")
        if slug.strip() and branch.strip():
            parsed[_normalise_slug(slug)] = branch.strip()
    return parsed


def resolve_pinned_branch(repo: str, entries: list[str]) -> str:
    """The one branch *repo* is pinned to, or ``""`` when it is not declared.

    *repo* may be a slug (``group/sub/name``), a full remote URL (SSH or HTTPS), a
    local path, or the BARE repo name that ``Ticket.repos`` carries — every
    producer that reaches this gate spells the repo differently, and requiring one
    spelling would silently unpin the callers that use another. The Bash gate
    resolves a full remote URL; the provisioner has only ``"widget-core"``.

    Matching is therefore path-suffix-wise in BOTH directions: a declaration of
    ``group/widget-core`` matches the longer
    ``git@example.com:org/group/widget-core.git`` and
    the shorter ``widget-core``. Two declared repos sharing a basename would both
    match a bare name — the same basename ambiguity ``find_clone_path`` already
    warns about — so declare the distinguishing path segment when that happens.
    """
    if not (pinned := parse_single_branch_repos(entries)):
        return ""
    candidate = _normalise_slug(repo)
    if not candidate:
        return ""
    for slug, branch in pinned.items():
        if candidate == slug or candidate.endswith(f"/{slug}") or slug.endswith(f"/{candidate}"):
            return branch
    return ""


def check_branch_admitted(branch: str, *, pinned_branch: str) -> SingleBranchFinding | None:
    """Return a finding when *branch* is a second branch in a pinned repo, else None.

    The provisioner's entry point. An unpinned repo (``pinned_branch == ""``)
    always admits, so the gate is inert everywhere it was not declared.
    """
    if not pinned_branch or not branch.strip():
        return None
    if branch.strip() == pinned_branch:
        return None
    return SingleBranchFinding(surface="provision", target=branch.strip())


def find_second_branch_creation(
    command: str, *, pinned_branch: str, current_branch: str = ""
) -> SingleBranchFinding | None:
    """Return a finding when *command* creates or publishes a second branch, else None.

    *current_branch* is the branch the target repo has checked out, supplied by the
    caller that can resolve it; ``""`` means unresolved and keeps the pre-existing
    allow. It is what makes a refspec-less ``git push`` decidable — see
    :func:`_push_finding`.

    Fails OPEN (``None``) on an unparsable command and on an unpinned repo — a
    gate that blocks what it cannot read teaches its operator to disable it.
    """
    if not pinned_branch:
        return None
    for segment in _segments(command):
        call = _git_call(segment)
        if call is None:
            continue
        subcommand, args = call
        if finding := _classify(subcommand, args, pinned_branch, current_branch):
            return finding
    return None


def deny_reason(finding: SingleBranchFinding, *, pinned_branch: str, repo: str = "") -> str:
    """The FAIL-LOUD deny message naming the rule, the pinned branch, and the way forward."""
    where = f" in `{repo}`" if repo else ""
    what = {
        "worktree": "creating a SECOND WORKTREE",
        "branch": f"creating a SECOND BRANCH (`{finding.target}`)",
        "push": f"pushing a SECOND BRANCH (`{finding.target}`)",
        "provision": f"provisioning a worktree on a SECOND BRANCH (`{finding.target}`)",
    }.get(finding.surface, f"creating `{finding.target}`")
    return (
        f"BLOCKED: {what}{where}. This repo is declared SINGLE-BRANCH: while its "
        f"integration PR is open, `{pinned_branch}` is the ONLY branch and the ONLY "
        "worktree, and every change — yours included — lands there.\n"
        "A side branch here is not a parallel lane, it is duplicated work: the "
        "recorded outcome was two branches re-implementing a fix already in "
        "flight on the integration branch.\n"
        f"Do this instead: commit onto `{pinned_branch}` in the existing checkout, "
        "and let the open PR carry it.\n"
        f"When the integration PR MERGES, the rule ends by removing this repo's "
        "entry from the `single_branch_repos` setting — not by working around the "
        "gate:\n"
        "  t3 <overlay> config_setting set single_branch_repos '<remaining entries>'\n"
        "Vetted one-off: append `[single-branch-ok: <reason>]` to the command."
    )


def _normalise_slug(repo: str) -> str:
    """A repo reference reduced to a comparable ``group/.../name`` slug.

    Strips a scheme, an SSH ``user@host:`` prefix, a trailing ``.git``, and any
    surrounding slashes, so every spelling of the same repo compares equal.
    """
    text = repo.strip().rstrip("/")
    for scheme in ("https://", "http://", "ssh://", "git://"):
        text = text.removeprefix(scheme)
    if ":" in text and not text.startswith("/"):
        text = text.rsplit(":", 1)[-1]
    text = text.removesuffix(".git")
    return text.strip("/")


def _segments(command: str) -> list[str]:
    segments = [command]
    for sep in _SEGMENT_SEPARATORS:
        segments = [piece for seg in segments for piece in seg.split(sep)]
    return [seg.strip() for seg in segments if seg.strip()]


def _git_call(segment: str) -> tuple[str, list[str]] | None:
    """``(subcommand, args)`` for a ``git`` invocation in *segment*, else None."""
    tokens = _safe_split(segment)
    try:
        index = tokens.index("git")
    except ValueError:
        return None
    cursor = index + 1
    while cursor < len(tokens):
        token = tokens[cursor]
        if not token.startswith("-"):
            return token, tokens[cursor + 1 :]
        base = token.split("=", 1)[0]
        cursor += 2 if base in _GLOBAL_OPTS_WITH_VALUE and "=" not in token else 1
    return None


def _safe_split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return []


def _classify(subcommand: str, args: list[str], pinned_branch: str, current_branch: str) -> SingleBranchFinding | None:
    if subcommand == "worktree":
        return _worktree_finding(args)
    if subcommand in {"checkout", "switch"}:
        return _created_branch_finding(subcommand, args, pinned_branch)
    if subcommand == "branch":
        return _branch_command_finding(args, pinned_branch)
    if subcommand == "push":
        return _push_finding(args, pinned_branch, current_branch)
    return None


def _worktree_finding(args: list[str]) -> SingleBranchFinding | None:
    """``git worktree add`` is refused; every other worktree subcommand allows.

    ``list``/``remove``/``prune`` are how a non-compliant repo is brought BACK to
    one worktree, so blocking them would make the gate defend the mess it exists
    to prevent. ``add`` is refused whatever branch it names — including
    ``--detach``, which is still a second working tree — because the pinned branch
    is already checked out in the one checkout that is allowed to exist.
    """
    if not args or args[0] != "add":
        return None
    return SingleBranchFinding(surface="worktree", target=_created_branch(args[1:]) or "detached")


def _created_branch_finding(subcommand: str, args: list[str], pinned_branch: str) -> SingleBranchFinding | None:
    """A ``checkout -b`` / ``switch -c`` that names a branch other than the pinned one."""
    flags = _CHECKOUT_CREATE_FLAGS if subcommand == "checkout" else _SWITCH_CREATE_FLAGS
    # Compare the flag BASE: ``--create=feat/x`` carries its value attached, and
    # matching the whole token missed exactly that spelling.
    if not any(arg.split("=", 1)[0] in flags for arg in args):
        return None
    created = _created_branch(args)
    if not created or created == pinned_branch:
        return None
    return SingleBranchFinding(surface="branch", target=created)


def _branch_command_finding(args: list[str], pinned_branch: str) -> SingleBranchFinding | None:
    """A bare ``git branch <name>`` that creates a branch other than the pinned one."""
    if any(arg.split("=", 1)[0] in _BRANCH_NON_CREATING_FLAGS for arg in args):
        return None
    created = next((arg for arg in args if not arg.startswith("-")), "")
    if not created or created == pinned_branch:
        return None
    return SingleBranchFinding(surface="branch", target=created)


def _push_finding(args: list[str], pinned_branch: str, current_branch: str) -> SingleBranchFinding | None:
    """A push whose DESTINATION branch is not the pinned one.

    This is the seam that actually stops a second MR appearing: a side branch
    nobody pushes is local clutter, a pushed one is a merge request. The
    destination is the right-hand side of a ``src:dst`` refspec, else the refspec
    itself.

    A push naming NO refspec publishes the CURRENT branch, so it is decided by
    *current_branch*: a repo declared single-branch while it still carries the side
    branches that motivated the declaration can be brought to one by ``checkout``
    (which creates nothing and is allowed) and then published by a bare ``git
    push`` — the exact second MR this gate exists to stop. An unresolved
    *current_branch* keeps the allow, so the gate never blocks what it cannot read.
    """
    positionals = [arg for arg in args if not arg.startswith("-")]
    if len(positionals) < _PUSH_POSITIONALS_WITH_REFSPEC:
        if current_branch and current_branch != pinned_branch:
            return SingleBranchFinding(surface="push", target=current_branch)
        return None
    for refspec in positionals[1:]:
        destination = _branch_from_ref(refspec.rpartition(":")[2] if ":" in refspec else refspec)
        if destination and destination != pinned_branch:
            return SingleBranchFinding(surface="push", target=destination)
    return None


def _branch_from_ref(ref: str) -> str:
    """A branch name from a possibly fully-qualified ref, or ``""`` for a non-branch ref."""
    stripped = ref.removeprefix("+").strip()
    if stripped.startswith("refs/heads/"):
        return stripped.removeprefix("refs/heads/")
    if stripped.startswith("refs/"):
        return ""
    return stripped


def _created_branch(args: list[str]) -> str:
    """The branch named by a ``-b``/``-B``/``-c``/``-C``/``--create`` flag, else ``""``.

    Handles both the separated (``-b name``) and the attached (``--create=name``)
    spellings; anything else yields ``""``, which the callers read as "no branch
    named here".
    """
    create_flags = _CHECKOUT_CREATE_FLAGS | _SWITCH_CREATE_FLAGS
    for index, arg in enumerate(args):
        base, _, attached = arg.partition("=")
        if base not in create_flags:
            continue
        if attached:
            return attached
        if index + 1 < len(args):
            return args[index + 1]
    return ""


__all__ = [
    "GATE_KEY",
    "SingleBranchFinding",
    "check_branch_admitted",
    "deny_reason",
    "find_second_branch_creation",
    "parse_single_branch_repos",
    "resolve_pinned_branch",
]
