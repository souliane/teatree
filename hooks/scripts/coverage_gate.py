"""Per-diff coverage gate (§17.6 gate 12) — trigger, target-repo scope, argv/finding.

A bare sibling of ``hook_router.py`` (the shrink-only god-module): the coverage
gate's pure helpers live here so the router stays under its module-health cap.

Two concerns. First, the merge-class TRIGGER (:func:`is_merge_class_command`):
the verb is detected on the quote/heredoc-stripped skeleton
(:func:`mr_cli_fields.strip_quoted_and_heredoc`), so a command that merely
MENTIONS ``gh pr create`` / ``glab mr create`` inside a quoted argument, a
commit message, or a heredoc body (a python script, a doc snippet) never fires
the gate — only a real create/un-draft invocation does.

Second, resolving the repo the GATED forge command actually publishes.
``handle_block_uncovered_diff`` shells ``t3 tool diff-coverage``; that CLI
defaults ``--repo`` to its own cwd. The cold PreToolUse hook inherits the
SESSION's cwd, which — when the gated command ships a DIFFERENT worktree via a
leading ``cd <worktree>`` — is not the PR's worktree at all. Measuring the
session cwd then flags uncovered lines from an unrelated worktree's diff. The
gate must measure the worktree the command runs in: its own leading ``cd``
(anchored on the ambient cwd when relative, ``~`` expanded), else the ambient
cwd, walked up to the enclosing repo root.

The publish TARGET is then resolved from the SHIP, never assumed from the
process working directory (:func:`measured_repo_is_publish_target`). An
EXPLICIT ``-R``/``--repo`` / api endpoint is authoritative: when it is not the
measured repo's own slug, the measured diff is some OTHER repo's unrelated work
and the measurement is skipped — a publish to repo X must never be gated on
uncommitted symbols in repo Y (§17.6.3 mis-scope). With NO explicit target the
gate does not fall back to "the cwd repo must be it"; it PROVES the measured
repo is the ship's source by resolving the push remote of the branch being
shipped (``--source-branch``/``-s`` for ``glab``, ``--head``/``-H`` for ``gh``,
else the measured repo's own HEAD). The branch knows where it is going; cwd is
incidental. When neither the branch nor its push destination resolves in the
measured repo, the measurement is about a different repo and is SKIPPED with a
stderr ``NOTE`` naming the reason and the ``-R`` remedy — never silently
measured against whatever the cwd happens to be. Resolution stays fail-open
throughout: uncertainty yields a skip, so the gate never wedges a create (#122).

Fail-open buys one hazard, and :func:`note_gate_skipped` pays it: a decline that
says nothing is indistinguishable from a clean measurement, so the gate can go
dark with no tell (#4004). EVERY path that declines without measuring names
itself on stderr — an unresolvable repo, a different publish target, a repo that
ships no branch, ``t3`` off PATH, a crashed measurement, an unparsable report.
A measurement that RAN stays silent, so a ``NOTE`` means exactly "did not
measure, and here is why". Pinned by
``tests/test_coverage_gate_never_silently_skips.py``, independently of this
module's own test file.
"""

import importlib
import json
import re
import shutil
import subprocess  # noqa: S404 — hook code legitimately shells `git` (mirrors hook_router).
import sys
from pathlib import Path
from typing import Final

from hooks.scripts.forge_api_detect import _is_api_create_endpoint_write, _is_existing_pr_metadata_only_edit
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.mr_cli_fields import extract_mr_target_repo, shlex_flag_value, strip_quoted_and_heredoc

# The moment a PR moves toward review/merge: ``gh pr ready`` (un-drafting) or a
# non-draft ``gh pr create`` / ``glab mr create`` / an api POST to a PR/MR
# collection endpoint. ``gh pr ready --undo`` (return-to-draft, the gate's own
# remediation) and ``--draft`` creation are excluded.
_GH_PR_READY_RE = re.compile(r"\bgh\s+pr\s+ready\b")
_PR_MR_CREATE_RE = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create)\b")
_FORGE_API_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_DRAFT_FLAG_RE = re.compile(r"(?:^|\s)(?:--draft|--undo)\b")


def is_merge_class_command(command: str) -> bool:
    """Whether ``command`` REALLY moves a PR toward review/merge.

    The verb regexes run on the quote/heredoc-stripped skeleton so a mere
    MENTION of ``glab mr create`` inside a quoted argument or a heredoc body (a
    commit message, a python script fed via ``<<EOF``) is not a trigger — the
    false-fire that gated an unrelated read-only script on the session repo's
    uncommitted diff. The api-endpoint classification
    (:func:`_is_api_create_endpoint_write`) still reads the ORIGINAL command:
    its endpoint/method arguments legitimately live inside quotes, which the
    skeleton strips.
    """
    skeleton = strip_quoted_and_heredoc(command)
    if _GH_PR_READY_RE.search(skeleton) or _PR_MR_CREATE_RE.search(skeleton):
        return not _DRAFT_FLAG_RE.search(skeleton)
    if _FORGE_API_RE.search(skeleton) and _is_api_create_endpoint_write(command):
        # Editing an existing PR's title/description creates nothing and merges
        # nothing, so it is not a merge-class write. Sibling gates REQUIRE such a
        # correction (conventional-commit first line, `## What` / `## Why`), and
        # this gate blocks a push — so firing on it makes satisfying one gate trip
        # another.
        if _is_existing_pr_metadata_only_edit(command):
            return False
        return not _DRAFT_FLAG_RE.search(skeleton)
    return False


def _slugs_name_same_repo(a: str, b: str) -> bool:
    """Whether two repo slugs name the same repo, host-qualification-symmetric.

    Either side may be bare (``owner/repo``) or host-qualified
    (``host/owner/repo``, the form ``slug_for_cwd`` returns); the shorter form
    must equal the trailing segments of the longer, case-insensitively, and at
    least ``owner/repo`` (two segments) must overlap — a single-segment value
    cannot identify a repo.
    """
    sa = [seg for seg in a.lower().removesuffix(".git").split("/") if seg]
    sb = [seg for seg in b.lower().removesuffix(".git").split("/") if seg]
    overlap = min(len(sa), len(sb))
    if overlap < 2:  # noqa: PLR2004 — owner/repo needs two path segments
        return False
    return sa[-overlap:] == sb[-overlap:]


# The branch a create command ships. ``glab mr create`` names it with
# ``-s``/``--source-branch``; ``gh pr create`` with ``-H``/``--head`` (whose
# ``<user>:<branch>`` cross-fork form carries an owner prefix). Read as exact
# shlex TOKENS, never a substring regex, so a flag letter inside another
# argument cannot be mistaken for the ship's branch.
_GLAB_MR_CREATE_RE = re.compile(r"\bglab\s+mr\s+create\b")
_GH_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")
_GLAB_SOURCE_BRANCH_FLAGS: Final[tuple[str, ...]] = ("--source-branch", "-s")
_GH_HEAD_BRANCH_FLAGS: Final[tuple[str, ...]] = ("--head", "-H")

# A git probe that hangs blocks the user; these are local ref/config reads.
_GIT_PROBE_TIMEOUT_S: Final[int] = 5

# The shelled `t3 tool diff-coverage` walks a whole diff, so it gets a wider
# budget than the ref reads above — still bounded, since a cold hook blocks the user.
_MEASUREMENT_TIMEOUT_S: Final[int] = 30


def note_gate_skipped(reason: str) -> None:
    """Emit a one-line diagnostic NOTE so a skip is never silent.

    Mirrors the banned-terms gate's unknown-slug NOTE (#1657): a gate that
    declines to measure must say WHY, or the next reader cannot tell a
    deliberate out-of-scope skip from a gate that quietly stopped working.

    Public because ``handle_block_uncovered_diff`` owns one fail-open branch of
    its own — the shelled measurement crashing or timing out — and a second
    message shape for the same event is how the two drift apart (#4004).
    """
    sys.stderr.write(f"NOTE: coverage gate 12 skipped — {reason}.\n")


def _git_probe(repo_dir: Path, args: list[str]) -> str | None:
    """Return stripped stdout of ``git -C <repo_dir> <args>``, or ``None`` on any failure."""
    try:
        result = subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            ["git", "-C", str(repo_dir), *args],  # noqa: S607 — git resolved on PATH, as everywhere else in-hook
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def shipped_branch(command: str) -> str | None:
    """The branch a create command ships, or ``None`` when it names none.

    ``None`` means "the command did not say" — the caller then falls back to
    the measured repo's own checked-out branch, which is what ``glab``/``gh``
    themselves default to. A ``gh`` cross-fork ``<user>:<branch>`` head keeps
    only the branch part; the owner half names the head REPO, not a ref.
    """
    skeleton = strip_quoted_and_heredoc(command)
    if _GLAB_MR_CREATE_RE.search(skeleton):
        flags = _GLAB_SOURCE_BRANCH_FLAGS
    elif _GH_PR_CREATE_RE.search(skeleton):
        flags = _GH_HEAD_BRANCH_FLAGS
    else:
        return None
    for flag in flags:
        parsed, value = shlex_flag_value(command, flag)
        if parsed and value:
            return value.split(":", 1)[-1]
    return None


def repo_ships_branch(repo_dir: Path, branch: str | None) -> bool:
    """Whether *repo_dir* is the working tree the ship comes FROM.

    The proof the gate needs before measuring *repo_dir*: the branch being
    shipped is a local branch HERE, and it has somewhere to be pushed. Both
    halves matter — a branch name resolved against the WRONG repo is exactly
    the mis-scope this replaces, and a branch with no push destination is not a
    ship at all. ``branch`` is ``None`` when the command named none, and then
    the repo's own checked-out branch is the ship (what ``glab``/``gh``
    default to); a detached HEAD names no branch and is unprovable.

    Push-remote resolution follows ``git``'s own order —
    ``branch.<name>.pushRemote``, then ``remote.pushDefault``, then
    ``branch.<name>.remote``, then ``origin`` — so a fork workflow (the branch
    pushes to a fork while the MR targets upstream) still resolves and stays
    enforced. Any probe failure is ``False``: unprovable is not measured.
    """
    branch = branch or _git_probe(repo_dir, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if not branch:
        return False
    if _git_probe(repo_dir, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]) is None:
        return False
    remote = (
        _git_probe(repo_dir, ["config", "--get", f"branch.{branch}.pushRemote"])
        or _git_probe(repo_dir, ["config", "--get", "remote.pushDefault"])
        or _git_probe(repo_dir, ["config", "--get", f"branch.{branch}.remote"])
        or "origin"
    )
    return _git_probe(repo_dir, ["remote", "get-url", "--push", remote]) is not None


def _explicit_target_is_measured_repo(target: str, repo_dir: Path) -> bool:
    """Whether an EXPLICIT ``-R``/api *target* names *repo_dir*'s own repo.

    An unexpanded ``$`` in the target, or a crash resolving the measured slug,
    keeps the established scope (fail-open, #122). An UNRESOLVABLE measured slug
    skips: the measurement cannot be proven to be about the published repo.
    """
    if "$" in target:
        return True
    try:
        with teatree_src_on_path():
            repo_visibility = importlib.import_module("teatree.hooks._repo_visibility")
            measured = repo_visibility.slug_for_cwd(repo_dir)
    except Exception:  # noqa: BLE001 — cold hook must stay crash-proof; keep the established scope
        return True
    if not measured:
        note_gate_skipped(f"the publish target is {target} but {repo_dir} has no resolvable repo slug")
        return False
    if _slugs_name_same_repo(target, measured):
        return True
    note_gate_skipped(f"the publish target is {target}, not the measured repo {measured}")
    return False


def measured_repo_is_publish_target(command: str, repo_dir: Path | None) -> bool:
    """Whether *repo_dir* (the repo about to be measured) IS the command's publish target.

    Two sources of truth, in order, and NEITHER is the process working
    directory — measuring cwd because nothing else was named is the mis-scope
    (§17.6.3) this resolves.

    1. An EXPLICIT literal target (``-R``/``--repo``, an api endpoint —
        :func:`mr_cli_fields.extract_mr_target_repo`) is authoritative. It is
        compared against *repo_dir*'s own git-remote slug: a cross-repo ship
        (``glab mr create -R other-org/other-repo`` issued from an unrelated
        clone) would otherwise measure the SESSION repo's diff and deny the
        create on symbols the published repo never sees. An unexpanded ``$`` in
        the target, or a crash resolving the measured slug, keeps the
        established scope (fail-open, #122); an UNRESOLVABLE measured slug
        skips — the measurement cannot be proven to be about the published repo.

    2. With no explicit target, the ship itself decides: *repo_dir* must be the
        working tree the branch is shipped FROM (:func:`repo_ships_branch`).
        This is the case that used to return ``True`` unconditionally — "no
        target named, so the cwd repo must be it" — which denied a markdown-only
        ship of one repo on dozens of uncovered symbols belonging to whichever
        repo the session happened to sit in.

    ``False`` — SKIP the measurement — whenever the target cannot be resolved
    at all (no *repo_dir*, an unknown slug, an unprovable branch), always with
    a :func:`note_gate_skipped` saying why. Skipping is the right failure here
    rather than denying: this is a cold PreToolUse hook whose contract is that a
    broken or ambiguous environment must never wedge a create (#122), and a gate that
    denies on evidence from an unrelated codebase gets routed around instead of
    resolved — which protects nothing at all.
    """
    if repo_dir is None:
        note_gate_skipped("no repo resolved for the command (no leading `cd` target on disk, no usable cwd)")
        return False
    target = extract_mr_target_repo(command)
    if target:
        return _explicit_target_is_measured_repo(target, repo_dir)
    if repo_ships_branch(repo_dir, shipped_branch(command)):
        return True
    note_gate_skipped(
        f"{repo_dir} does not ship the branch being published, so its diff is a different "
        "repo's work; name the target with `-R <owner>/<repo>` to scope the gate explicitly"
    )
    return False


# Byte-identical to ``teatree.utils.diff_coverage.UNREFERENCED_SYMBOL_IMPORT_HINT``; this
# cold-import-safe sibling cannot import ``teatree`` at module top, so the string is
# duplicated and pinned equal by a drift-guard test (test_block_uncovered_diff_hook.py).
_UNREFERENCED_SYMBOL_IMPORT_HINT = (
    "    workaround: this check reads a changed test's import statements only — a "
    "`module.symbol(...)` call does not count as a reference; when the symbol is already "
    "exercised, add `from module import symbol` to a changed test to make the reference visible"
)


def coverage_gate_repo_dir(command: str, cwd: str | None) -> Path | None:
    """Return the repo root whose diff the gated forge command should be measured against.

    The gated ``gh pr create`` / ``glab mr create`` / ``gh pr ready`` runs in its
    own leading ``cd <dir>`` (a cross-worktree ship), NOT the session cwd the cold
    hook inherits. Resolving that ``cd`` — ``~`` expanded, anchored on the ambient
    *cwd* when relative — and walking up to the enclosing repo root keeps the
    coverage measurement on the PR's OWN worktree, never a sibling worktree's
    stray diff.

    A leading ``cd`` that does NOT land on a real directory (an unexpanded
    ``$VAR``, a typo) yields ``None``, never the ambient cwd. Anchoring an
    unresolvable path on the ambient cwd and walking UP finds the AMBIENT repo's
    ``.git`` — a confident, wrong answer that reads exactly like a correct one,
    and the mechanism that measured the session repo for a ship of another. The
    ``~``-prefixed form was such a path.

    Both rules — the ``~`` expansion and the three-valued landing verdict — are
    :func:`teatree.hooks._commit_repo_dir.leading_cd_target`'s, shared with the
    banned-terms commit carve-out that resolves the same ``cd`` for the privacy
    decision. Restating them here is what let the two gates drift.

    ``None`` also when neither a leading ``cd`` nor an ambient *cwd* resolves;
    the caller reads ``None`` as "cannot tell" and skips, rather than measuring
    whatever directory the hook process happens to be in.
    """
    ambient = Path(cwd) if cwd else None
    try:
        with teatree_src_on_path():
            # ``teatree.hooks._commit_repo_dir`` is a private module reached from a
            # cold-hook sibling; import it dynamically so the parsers stay a single
            # source of truth without a static private-name import.
            commit_repo_dir = importlib.import_module("teatree.hooks._commit_repo_dir")
            landed = commit_repo_dir.leading_cd_target(command, ambient)
            target = ambient if landed is None else landed
            if not isinstance(target, Path):
                # The ``UNRESOLVABLE_REPO_DIR`` str sentinel (a ``cd`` naming no real
                # directory) or ``None`` (no ``cd`` and no ambient cwd) — neither pins
                # a repo, so skip rather than measure the session's own diff.
                return None
            return commit_repo_dir.git_root_for_dir(target) or target
    except Exception:  # noqa: BLE001 — cold hook must stay crash-proof; degrade to the ambient cwd
        return ambient


def diff_coverage_argv(repo_dir: Path | None) -> list[str] | None:
    """Return the ``t3 tool diff-coverage --json`` argv keyed to *repo_dir*, or ``None``.

    ``None`` when ``t3`` is not on PATH (the gate then fails open), announced —
    a cold hook inherits a restricted PATH, so this is the shape in which gate 12
    stops firing for every create on the box at once (#4004). ``--repo`` is
    appended only when *repo_dir* resolved, so a cwd-relative run (the historical
    behaviour) is preserved when no target could be pinned.
    """
    t3_bin = shutil.which("t3")
    if t3_bin is None:
        note_gate_skipped("`t3` is not on PATH, so the diff cannot be measured")
        return None
    argv = [t3_bin, "tool", "diff-coverage", "--json"]
    if repo_dir is not None:
        argv += ["--repo", str(repo_dir)]
    return argv


def diff_coverage_finding(stdout: str) -> str | None:
    """Return a deny reason iff *stdout* is a report JSON with ``passes`` false.

    The fail-open discriminator (#122). ``t3 tool diff-coverage --json`` emits
    exactly ``{"passes": ..., "uncovered": [...], "unreferenced_symbols": [...]}``
    on a successful measurement. A crash (e.g. the dev-only ``coverage`` module
    missing from the installed ``t3`` env) produces a traceback on stderr and no
    parseable JSON on stdout — so anything that is not a well-formed report with
    ``passes is False`` is "not a finding" and the caller fails open.

    Returns the human-readable finding summary when there IS a genuine finding,
    else ``None`` (clean, crashed, or unparsable). The three no-verdict shapes
    announce themselves and the two verdict-carrying ones stay silent, so a
    ``NOTE`` distinguishes "did not measure" from "measured and passed" (#4004).
    """
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        note_gate_skipped("`t3 tool diff-coverage --json` produced no parseable report")
        return None
    if not isinstance(report, dict):
        note_gate_skipped("the diff-coverage report is not a JSON object")
        return None
    verdict = report.get("passes")
    if verdict is True:
        return None
    if verdict is not False:
        note_gate_skipped("the diff-coverage report carries no `passes` verdict")
        return None
    rows = [
        f"  uncovered new lines in {entry.get('path')}: {entry.get('lines')}"
        for entry in (report.get("uncovered") or [])
        if isinstance(entry, dict)
    ]
    symbols = report.get("unreferenced_symbols") or []
    if symbols:
        rows.extend(
            (
                f"  new production symbols not referenced by any changed test: {sorted(symbols)}",
                _UNREFERENCED_SYMBOL_IMPORT_HINT,
            )
        )
    return "\n".join(rows)


def coverage_finding_for_command(command: str, cwd: str | None) -> str | None:
    """The gate's verdict for an already-triggered *command*: a deny reason, or ``None``.

    Every fail-open branch between the merge-class trigger and the deny lives
    here, so each one sits next to the :func:`note_gate_skipped` that explains it
    (#4004) — the router keeps only the trigger and the deny. Scattering these
    across the two modules is how one of them stayed silent.
    """
    repo_dir = coverage_gate_repo_dir(command, cwd)
    if not measured_repo_is_publish_target(command, repo_dir):
        return None
    argv = diff_coverage_argv(repo_dir)
    if argv is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_MEASUREMENT_TIMEOUT_S,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        note_gate_skipped(f"the diff-coverage measurement did not complete ({type(exc).__name__})")
        return None
    return diff_coverage_finding(result.stdout or "")
