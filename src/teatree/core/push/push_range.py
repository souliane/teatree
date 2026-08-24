"""What the next push newly EXPOSES — HEAD minus every already-public tip.

``t3 fast-push`` pushes with the hook chain bypassed, so ``git push`` delivers every
committed-but-unpushed commit on the branch. The in-process leak gates it runs instead
read ``git diff --cached``, which is the STAGED delta and nothing else: a secret
committed in an earlier turn, with nothing staged now, was pushed with ``ok: True`` and
``findings: []`` while all four gates reported executed. The pre-push hook the engine
replaces (``scripts/hooks/refuse-public-push-with-leak.sh``) has always judged the whole
range. This module is the range that closes the gap.

The set is the hook's, ported rather than re-derived, because the obvious spelling is
wrong in a way that only shows up in production. A linear ``<remote-sha>..HEAD`` span is
inflated by a merge-forward with the whole of ``main`` — already public, immutable, and
full of findings and squash-merge identities that are not this branch's to answer for
(#3523). So the range is HEAD ``--not`` every already-public tip:

``origin/<default>`` always, and the branch's OWN remote-tracking tip when it exists.
That second tip is the port of the hook's ``_remote_sha_is_trusted_base``: the hook
trusts a reported remote sha only once it is confirmed to be the current tip of
``refs/remotes/<remote>/<branch>``, and reading that ref directly IS that confirmation.
A force-push whose old tip is no longer an ancestor of HEAD still gets scanned — the
rewritten commits are reachable from no public ref.

The CONTENT is the hook's own ``git log --patch --cc <range>`` over that set, not a
two-endpoint ``git diff``. A three-dot ``<default-tip>...HEAD`` diff gets the
merge-forward right (the merge base IS the merged-in ``main`` tip) but re-scans the
branch's OWN already-pushed commits, so one flagged line in a long-lived branch's
published history refuses every subsequent push of it forever — observed on
``test_an_already_pushed_branch_commit_is_not_re_judged``. Per-commit patches over the
``--not`` set are subtractive on BOTH axes at once. ``--cc`` keeps a merge's conflict
resolutions — content in neither parent — in scope while leaving the already-public
content the merge carried over out of it.

Everything is read ONCE, at resolution, so the gates consume plain data and there is a
single failure point. An unresolvable range returns ``None`` and the caller refuses —
never skips. That is the posture ``FastPusher._branch_guard_finding`` already takes on an
unresolvable default branch: a bypassed hook chain leaves no second net to fail open into.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail


@dataclass(frozen=True, slots=True)
class PushRange:
    """The already-public tips this push is measured against, and what it adds to them."""

    public_tips: tuple[str, ...]
    diff_text: str
    commit_messages: str
    identities: tuple[str, ...]

    @classmethod
    def resolve(cls, repo: Path, *, branch: str, default_branch: str) -> "PushRange | None":
        default_tip = _commit_sha(repo, f"refs/remotes/origin/{default_branch}")
        if default_tip is None:
            return None
        branch_tip = _commit_sha(repo, f"refs/remotes/origin/{branch}")
        tips = (default_tip,) if branch_tip is None else (default_tip, branch_tip)
        rev_args = ["HEAD", "--not", *tips]

        diff = _git_output(repo, ["log", "--format=", "--patch", "--cc", *rev_args])
        messages = _git_output(repo, ["log", "--format=%B", *rev_args])
        identities = _git_output(repo, ["log", "--format=%ae%n%ce", *rev_args])
        if diff is None or messages is None or identities is None:
            return None
        return cls(
            public_tips=tips,
            diff_text=diff.rstrip("\n"),
            commit_messages=messages,
            identities=tuple(line for line in identities.splitlines() if line),
        )


def _commit_sha(repo: Path, ref: str) -> str | None:
    result = run_allowed_to_fail(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        expected_codes=None,
        cwd=repo,
    )
    return result.stdout.strip() or None


def _git_output(repo: Path, args: list[str]) -> str | None:
    result = run_allowed_to_fail(["git", *args], expected_codes=None, cwd=repo)
    return result.stdout if result.returncode == 0 else None
