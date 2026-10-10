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

Everything is read ONCE, at resolution, so the gates consume plain data and there is a
single failure point. A range git cannot walk returns ``None`` and the caller refuses —
never skips: a bypassed hook chain leaves no second net to fail open into.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks.remote_holds import remote_holdings
from teatree.utils.run import run_allowed_to_fail, spawn_byte_pipe

_REMOTE = "origin"

#: Repo-local git state (a textconv driver, an external diff) rewrites the text of a diff; the gates read the raw one.
READ_FLAGS = ("--no-textconv", "--no-ext-diff", "--no-color")


@dataclass(frozen=True, slots=True)
class PushRange:
    """The already-public tips this push is measured against, and what it adds to them."""

    public_tips: tuple[str, ...]
    diff_text: str
    commit_messages: str
    identities: tuple[str, ...]

    @classmethod
    def resolve(cls, repo: Path) -> "PushRange | None":
        tips = _remote_tips(repo)
        rev_args = ["HEAD", "--not", *tips] if tips else ["HEAD"]

        diff = _git_output(repo, ["log", *READ_FLAGS, "--format=", "--patch", "--cc", *rev_args])
        messages = _git_output(repo, ["log", "--encoding=UTF-8", "--format=%B", *rev_args])
        identities = _git_output(repo, ["log", "--encoding=UTF-8", "--format=%ae%n%ce", *rev_args])
        stored = _stored_messages(repo, rev_args)
        if diff is None or messages is None or identities is None or stored is None:
            return None
        return cls(
            public_tips=tips,
            diff_text=diff.rstrip("\n"),
            commit_messages=_with_stored(messages, stored),
            identities=tuple(line for line in identities.splitlines() if line),
        )


def _commit_object(repo: Path, sha: str) -> bytes | None:
    pipe = spawn_byte_pipe(["git", "--no-replace-objects", "-C", str(repo), "cat-file", "commit", sha])
    try:
        pipe.stdin.close()
        data = pipe.stdout.read()
    finally:
        pipe.stdout.close()
        code = pipe.process.wait()
    return data if code == 0 else None


def _stored_messages(repo: Path, rev_args: list[str]) -> list[str] | None:
    """Each commit's message exactly as the object stores it, which is what the remote holds.

    The ``encoding`` header travels with the pushed commit, so ``i18n.commitEncoding=UTF-16LE`` on plain bytes makes
    ``git log`` decode them into noise while the message is still there to read.
    """
    shas = _git_output(repo, ["rev-list", *rev_args])
    if shas is None:
        return None
    messages: list[str] = []
    for sha in shas.split():
        data = _commit_object(repo, sha)
        if data is None:
            return None
        raw = data.partition(b"\n\n")[2]
        try:
            messages.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            messages.append(raw.decode("latin-1"))
    return messages


def _with_stored(decoded: str, stored: list[str]) -> str:
    """*decoded* followed by the stored-message lines it does not already carry."""
    known = set(decoded.splitlines())
    extra = [line for message in stored for line in message.splitlines() if line not in known]
    return decoded + "\n".join(extra) + "\n" if extra else decoded


def _remote_tips(repo: Path) -> tuple[str, ...]:
    push_url = _git_output(repo, ["remote", "get-url", "--push", _REMOTE])
    if not push_url or not push_url.strip():
        return ()
    return remote_holdings(repo, remote_url=push_url.strip(), remote_name=_REMOTE).tips


def _git_output(repo: Path, args: list[str]) -> str | None:
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    result = run_allowed_to_fail(["git", *args], expected_codes=None, cwd=repo, env=env)
    return result.stdout if result.returncode == 0 else None
