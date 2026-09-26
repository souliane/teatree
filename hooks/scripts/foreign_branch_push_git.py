"""Targeted, live Git probes for the foreign-branch push gate.

This leaf owns the filesystem and network mechanics; the sibling gate owns the
ownership decision and refusal text. Every probe carries WHY it could not
answer — its exit code and stderr, a timeout, or a ``git`` that could not be
executed at all — so the fail-closed caller cannot mistake an unavailable
answer for an absent ref. ``ok=True, out=""`` is the branch genuinely absent,
which is ours to create; :attr:`GitProbe.ok` keeps that apart from a failure.

Every mutation of untrusted text here operates on ALREADY-REDACTED text: both
:func:`bounded` and :func:`_one_line` scrub before they clip or join.
"""

import re
import shlex
import subprocess  # noqa: S404 -- hook code legitimately shells out to Git.
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hooks.scripts.credential_redaction import clip, redact_credentials
from hooks.scripts.managed_repo import teatree_src_on_path

_HEADS_PREFIX: Final[str] = "refs/heads/"
_LS_REMOTE_COLUMN_COUNT: Final[int] = 2
_LS_REMOTE_TIMEOUT_S: Final[float] = 12.0
_GIT_TIMEOUT_S: Final[float] = 5.0
# A refusal is read in a terminal, so git's output — stderr and stdout alike — is truncated past this, not dropped.
_MAX_QUOTED_CHARS: Final[int] = 400
# `git config --get` exits 1 for a key nobody set, which is an ANSWER and not a failure.
_CONFIG_KEY_UNSET: Final[int] = 1
# `push.default` values that write every same-named branch, or none, but never one named.
_UNNAMED_PUSH_DEFAULTS: Final[frozenset[str]] = frozenset({"matching", "nothing"})


@dataclass(frozen=True, slots=True)
class GitProbe:
    """One probe's answer, and — when it has none — why.

    ``code`` is ``None`` exactly when the probe never produced a return code:
    it timed out, or ``git`` could not be executed. ``err`` then carries that
    reason instead of git's stderr, so :func:`probe_cause` can tell a run that
    FAILED from one that never RAN.
    """

    ok: bool
    out: str
    code: int | None = None
    err: str = ""


def push_work_dir(tokens: list[str], cwd: str) -> tuple[str, str]:
    """Resolve the repo selected by this push's ``-C``/``--git-dir`` flags."""
    try:
        with teatree_src_on_path():
            from teatree.hooks._commit_repo_dir import (  # noqa: PLC0415, PLC2701 -- cold-hook import
                UNRESOLVABLE_REPO_DIR,
                resolve_commit_dir,
            )

            landing = resolve_commit_dir(shlex.join(tokens), Path(cwd) if cwd else None)
        if landing == UNRESOLVABLE_REPO_DIR or not isinstance(landing, Path):
            raise ValueError  # noqa: TRY301 -- one fail-closed return for every unresolved shape.
    except Exception:  # noqa: BLE001 -- an unresolvable target is UNKNOWN, which REFUSES.
        return cwd, "the repository selected by the git global options"
    return str(landing), ""


def git_probe(work_dir: str, *args: str, timeout: float = _GIT_TIMEOUT_S) -> GitProbe:
    """Run a bounded Git probe in the targeted repo; return ``ok=False`` on failure."""
    try:
        proc = subprocess.run(  # noqa: S603 -- trusted internal subprocess; fixed argv, no shell.
            # Use the same Git from PATH as the push this gate judges.
            ["git", "-C", work_dir or ".", "--no-optional-locks", *args],  # noqa: S607 -- see above.
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GitProbe(ok=False, out="", err=f"timed out after {timeout:g}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitProbe(ok=False, out="", err=f"could not run git: {exc}")
    return GitProbe(
        ok=proc.returncode == 0,
        out=proc.stdout.strip(),
        code=proc.returncode,
        err=_one_line(proc.stderr),
    )


def _one_line(stderr: str) -> str:
    """*stderr* as one bounded line, scrubbed BEFORE the join and before any clip.

    ONE scrub, not one per line: a run never spans a newline, so the per-line loop
    cost the whole of stderr rather than the scan cap, against a hook with no
    wall-clock budget. It was also weaker, since `str.splitlines` splits on six
    characters the scanner treats as no boundary at all.
    """
    scrubbed = redact_credentials(stderr)
    return clip("; ".join(line.strip() for line in scrubbed.splitlines() if line.strip()), _MAX_QUOTED_CHARS)


def bounded(text: str) -> str:
    """*text* redacted, then capped at :data:`_MAX_QUOTED_CHARS`.

    In that order, and the order is the point: clipping first severs the `@` a
    userinfo needs and the value a pair needs, so a scrub applied to the assembled
    refusal afterwards matches nothing and the surviving prefix of a token is
    published.
    """
    return clip(redact_credentials(text), _MAX_QUOTED_CHARS)


def probe_cause(probe: GitProbe) -> str:
    """Why *probe* has no answer, in the words of whatever refused to give one.

    The one place a failed probe becomes readable text, so the gate's refusals
    cannot drift on how a timeout reads versus an ``exit 128``. The rendering is
    deliberately not ``name=value``: the scrub redacts every pair it is handed,
    and a diagnosis that redacts itself is no diagnosis.
    """
    if probe.code is None:
        return probe.err or "the probe never ran, and said nothing"
    if probe.code == 0:
        # A probe that EXITED 0 and answered unusably: the code is not the failure, and
        # beside the refusal it explains, `exit 0` reads as a success.
        return probe.err or "it exited 0, and said nothing this gate could read"
    return f"exit {probe.code}: {probe.err}" if probe.err else f"exit {probe.code} with no stderr"


def repository_at(work_dir: str) -> GitProbe:
    """Whether *work_dir* resolves to a repository at all, else git's own cause.

    Asked BEFORE any remote probe, because ``origin`` is a repository-relative
    name: outside a repository there is no remote for ``ls-remote`` to resolve,
    and the resulting ``exit 128`` reads exactly like an unreachable forge. The
    gate needs the two apart to say something the reader can act on.

    ``--git-dir`` rather than ``--show-toplevel`` because the question is
    existence, not location, and every shape a push may name must answer yes:
    a work tree, a SUBDIR of one (git walks up on its own, which is why nothing
    here normalises the dir), a bare repo, and the ``.git`` dir itself — the
    last being what a ``git --git-dir=<repo>/.git push`` resolves to, where
    ``--show-toplevel`` fails with "this operation must be run in a work tree"
    and would have refused a push the gate is meant to judge on authorship.
    """
    return git_probe(work_dir, "rev-parse", "--git-dir")


def remote_branch_oid(work_dir: str, remote: str, branch: str) -> GitProbe:
    """Return the advertised branch OID, empty when absent, or ``ok=False``."""
    ref = f"{_HEADS_PREFIX}{branch}"
    probe = git_probe(work_dir, "ls-remote", "--heads", remote, ref, timeout=_LS_REMOTE_TIMEOUT_S)
    if not probe.ok or not probe.out:
        return probe
    for line in probe.out.splitlines():
        columns = line.split()
        if (
            len(columns) == _LS_REMOTE_COLUMN_COUNT
            and columns[1] == ref
            and re.fullmatch(r"[0-9a-fA-F]{40,64}", columns[0])
        ):
            return GitProbe(ok=True, out=columns[0].lower())
    # It ANSWERED, just not in a shape this parse trusts — a distinct failure
    # from a probe that never ran, and it must not read as one. `code` stays 0
    # (the run is what it reports); the err says so, since only a git that
    # SUCCEEDED reaches here and a bare `exit 0` would read as one that had not.
    return GitProbe(
        ok=False,
        out="",
        code=probe.code,
        err=(
            f"an `ls-remote` that exited 0 but advertised no parsable `{bounded(ref)}` line; "
            f"it said: {bounded(probe.out)!r}"
        ),
    )


@dataclass(frozen=True, slots=True)
class LiveRange:
    """A stable live remote range, or the probe that would not establish one.

    *probe* travels with *failed*: every step here is a git probe, and its rc and
    stderr are what tell an unreachable forge from a remote with no default branch
    from a fetch the credential was refused for. Naming the argv and dropping its
    answer is the shape the gate's ladder says it does not have.
    """

    base_branch: str
    base_oid: str
    failed: str
    probe: "GitProbe | None" = None


def live_remote_range(work_dir: str, remote: str, branch: str, branch_oid: str) -> LiveRange:
    """The live default-branch..branch range for *branch*, or which probe would not pin it."""
    base = git_probe(work_dir, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
    remote_prefix = f"{remote}/"
    if not base.ok or not base.out or not base.out.startswith(remote_prefix):
        return LiveRange("", "", f"`{remote}/HEAD` (the remote's default branch)", base)
    base_branch = base.out.removeprefix(remote_prefix)
    base_ref = remote_branch_oid(work_dir, remote, base_branch)
    if not base_ref.ok or not base_ref.out:
        failed = f"`git ls-remote --heads {remote} {base_branch}` (the live default branch)"
        return LiveRange("", "", failed, base_ref)
    for oid, label in ((base_ref.out, base_branch), (branch_oid, branch)):
        fetched = git_probe(
            work_dir,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            remote,
            f"{_HEADS_PREFIX}{label}",
            timeout=_LS_REMOTE_TIMEOUT_S,
        )
        still_live = remote_branch_oid(work_dir, remote, label)
        if not fetched.ok or not still_live.ok or still_live.out != oid:
            failed = f"`git fetch {remote} {label}` (a stable live remote commit)"
            return LiveRange("", "", failed, fetched if not fetched.ok else still_live)
    return LiveRange(base_branch, base_ref.out, "")


@dataclass(frozen=True, slots=True)
class PushTargets:
    """What a refspec-less push writes, as the repository's own config answers it."""

    refspecs: tuple[str, ...] = ()
    unpinnable: str = ""
    probe: "GitProbe | None" = None
    prefer_upstream: bool = True


def push_target_policy(work_dir: str, remote: str, config_overrides: tuple[str, ...]) -> PushTargets:
    """Which branches a refspec-less push writes, per `remote.<remote>.push` then `push.default`.

    The push's own `-c` pairs are REPLAYED, because `git -c push.default=matching push
    origin` writes every same-named branch — measured — and a probe reading only the
    config files would not see it. `remote.<remote>.push` wins when set: git ignores
    `push.default` entirely then.

    A `matching`/`nothing` default is REFUSED rather than enumerated, the same
    conservative answer `--all`/`--mirror` already draw for naming no branch. It costs a
    user with a global `matching` a refusal on every bare `git push`; enumerating
    (`for-each-ref` INTERSECT `ls-remote --heads`) costs a probe per branch.

    Not covered, stated rather than implied: an inline `VAR=value` assignment on the
    same command line (the parser drops those before the argv is read; variables already
    exported in the agent's environment ARE inherited by this probe and therefore seen),
    a `--config-env` key naming one, a `--repo <url>` remote that has no
    `remote.<name>.push` key at all, and a config file mutated between this probe and
    git's own read.
    """
    replay = tuple(token for pair in config_overrides for token in ("-c", pair))
    configured = git_probe(work_dir, *replay, "config", "--get-all", f"remote.{remote}.push")
    if refused := _unreadable(configured, f"`git config --get-all remote.{remote}.push` (this remote's refspecs)"):
        return refused
    if configured.out:
        refspecs = tuple(configured.out.splitlines())
        if any("*" in refspec for refspec in refspecs):
            return PushTargets(unpinnable=f"`remote.{remote}.push` is a wildcard, so the branches it writes")
        return PushTargets(refspecs=refspecs)
    default = git_probe(work_dir, *replay, "config", "--get", "push.default")
    if refused := _unreadable(default, "`git config --get push.default` (which branches a bare push writes)"):
        return refused
    if default.out in _UNNAMED_PUSH_DEFAULTS:
        return PushTargets(unpinnable=f"`push.default` is `{default.out}`, so the branches this push writes")
    return PushTargets(prefer_upstream=default.out != "current")


def _unreadable(probe: GitProbe, description: str) -> PushTargets | None:
    """A config probe that failed for any reason OTHER than an unset key."""
    return None if probe.ok or probe.code == _CONFIG_KEY_UNSET else PushTargets(unpinnable=description, probe=probe)
