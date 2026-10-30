"""What the next push newly EXPOSES — HEAD minus every commit the remote already holds.

``t3 fast-push`` pushes with the hook chain bypassed, so ``git push`` delivers every
committed-but-unpushed commit on the branch. The in-process leak gates it runs instead
read ``git diff --cached``, which is the STAGED delta and nothing else: a secret
committed in an earlier turn, with nothing staged now, was pushed with ``ok: True`` and
``findings: []`` while all four gates reported executed. The pre-push hook the engine
replaces (``scripts/hooks/refuse-public-push-with-leak.sh``) has always judged the whole
range. This module is the range that closes the gap.

What the remote holds comes from :mod:`teatree.hooks.remote_holds`, the one function the
hook shares: the heads the remote LISTS at the URL the push goes to, never a local
remote-tracking ref, which the pusher can write. It is HEAD ``--not`` those tips, not a
linear ``<remote-sha>..HEAD`` span, which a merge-forward inflates with the whole of
``main`` — already public, immutable, and full of findings and squash-merge identities
that are not this branch's to answer for (#3523). A remote that cannot be listed holds
nothing here, so the range gets wider and never narrower.

The CONTENT is the hook's own ``git log --patch --cc <range>`` over that set, not a
two-endpoint ``git diff``, which would re-scan the branch's OWN already-pushed commits
and refuse every later push of a long-lived branch over one flagged line forever.
``--cc`` keeps a merge's conflict resolutions — content in neither parent — in scope while
leaving the already-public content the merge carried over out of it.

The whole commit objects, the stored messages and the noreply cut come from :mod:`teatree.hooks.commit_objects`,
which the hook runs too, so the two readers cannot drift.

Everything is read ONCE, at resolution, so the gates consume plain data and there is a
single failure point. A range git cannot walk returns ``None`` and the caller refuses —
never skips: a bypassed hook chain leaves no second net to fail open into.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks.commit_objects import (
    READ_FLAGS,
    commit_objects,
    decode_robust,
    decoded_messages,
    without_the_noreply_domain,
)
from teatree.hooks.remote_holds import remote_holdings
from teatree.utils.run import run_allowed_to_fail

_REMOTE = "origin"


@dataclass(frozen=True, slots=True)
class PushRange:
    """The already-public tips this push is measured against, and what it adds to them."""

    public_tips: tuple[str, ...]
    diff_text: str
    commit_messages: str
    commit_objects: str
    commit_objects_blanked: str
    identities: tuple[str, ...]

    @classmethod
    def resolve(cls, repo: Path) -> "PushRange | None":
        tips = _remote_tips(repo)
        rev_args = ["HEAD", "--not", *tips] if tips else ["HEAD"]

        diff = _git_output(repo, ["log", *READ_FLAGS, "--format=", "--patch", "--cc", *rev_args])
        messages = decoded_messages(rev_args, repo)
        identities = _git_output(repo, ["log", "--encoding=UTF-8", "--format=%ae%n%ce", *rev_args])
        objects = commit_objects(rev_args, repo)
        if diff is None or messages is None or identities is None or objects is None:
            return None
        return cls(
            public_tips=tips,
            diff_text=diff.rstrip("\n"),
            commit_messages=decode_robust(messages),
            commit_objects="\n".join(decode_robust(obj) for obj in objects),
            commit_objects_blanked="\n".join(decode_robust(without_the_noreply_domain(obj)) for obj in objects),
            identities=tuple(line for line in identities.splitlines() if line),
        )


def _remote_tips(repo: Path) -> tuple[str, ...]:
    push_url = _git_output(repo, ["remote", "get-url", "--push", _REMOTE])
    if not push_url or not push_url.strip():
        return ()
    return remote_holdings(repo, remote_url=push_url.strip(), remote_name=_REMOTE).tips


def _git_output(repo: Path, args: list[str]) -> str | None:
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    result = run_allowed_to_fail(["git", *args], expected_codes=None, cwd=repo, env=env)
    return result.stdout if result.returncode == 0 else None


def _added_lines_from_diff(diff_text: str) -> dict[str, list[str]]:
    """Keep added hunk bodies, including lines beginning with ``++``."""
    by_path: dict[str, list[str]] = {}
    path = ""
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = "", False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
        elif in_hunk and line.startswith("+") and path:
            by_path.setdefault(path, []).append(line[1:])
    return by_path
