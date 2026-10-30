"""What a remote already holds, as commit shas this clone has — read from the remote, never from a tracking ref.

The commits a push newly exposes are the ones the REMOTE does not hold. A remote-tracking ref is a file the
pusher can write (``update-ref``, ``packed-refs``, ``git fetch . HEAD:refs/remotes/…``, a ``set-url`` plus
``fetch`` from another repository), so a range bounded by one can be emptied. This module is the one place
that answers the question, for the pre-push hook (through ``python -m``) and for ``t3 fast-push``.

The answer is the tip of every head ``git ls-remote --heads`` lists at the URL the push goes to, plus the sha
git reported for the ref being updated, keeping only commits this clone has. A listing is trusted only when it
comes from the place the push goes: ``url.<base>.insteadOf`` aimed at another repository with an identity
``pushInsteadOf`` keeps the push on the real remote while the listing reads the other one, so a URL that
git would rewrite for listing, or whose push URL is not the one given, is treated as unlistable. An unlistable
remote holds only what git reported, so the range to scan gets wider and never narrower.

Stdlib only: the hook runs it with ``python -S`` and ``PYTHONPATH`` at the checkout's own ``src``.
"""

import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

__all__ = ["RemoteHoldings", "remote_holdings"]

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_LISTING_TIMEOUT_S = 120
_LOCAL_TIMEOUT_S = 30
_LOW_SPEED = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")


@dataclass(frozen=True, slots=True)
class RemoteHoldings:
    """The commits the remote is known to hold, and whether its heads could be listed to find them."""

    tips: tuple[str, ...]
    listed: bool
    reason: str = ""


def _git(
    repo: Path,
    *args: str,
    timeout: int = _LOCAL_TIMEOUT_S,
    stdin: str = "",
    env_override: Mapping[str, str] | None = None,
) -> str | None:
    """Stdout of ``git *args`` in *repo*, or ``None`` when git failed, timed out or is missing."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {**os.environ, **(env_override or {}), "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = run_allowed_to_fail(
            [git, *args], expected_codes=None, cwd=repo, env=env, stdin_text=stdin, timeout=timeout
        )
    except (OSError, TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _why_a_listing_cannot_be_trusted(
    repo: Path, remote_url: str, remote_name: str, env_override: Mapping[str, str] | None = None
) -> str:
    """``""`` when a listing at *remote_url* comes from where the push goes, else why it would not."""
    pushed_to = _git(repo, "remote", "get-url", "--push", remote_name)
    if pushed_to is None:
        return "could not confirm the URL git pushes to (no remote of that name, or a push to a raw URL)"
    if pushed_to.strip() != remote_url:
        return "could not list the heads: the push URL of the remote differs from the URL git passed"
    listed_at = _git(repo, "ls-remote", "--get-url", remote_url, env_override=env_override)
    if listed_at is None or listed_at.strip() != remote_url:
        return (
            "could not list the heads at the URL git pushes to: a url.<base>.insteadOf or pushInsteadOf "
            "setting would send the listing to another repository"
        )
    return ""


def _commits_held_here(repo: Path, shas: list[str]) -> list[str]:
    if not shas:
        return []
    checked = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin="\n".join(shas) + "\n")
    held = [line.split()[0] for line in (checked or "").splitlines() if line.endswith(" commit")]
    return list(dict.fromkeys(held))


def remote_holdings(
    repo: Path,
    *,
    remote_url: str,
    remote_name: str,
    reported_shas: Sequence[str] = (),
    env_override: Mapping[str, str] | None = None,
) -> RemoteHoldings:
    """The commits the remote at *remote_url* holds that *repo* has, with *reported_shas* (git's own word) added."""
    reported = [sha for sha in reported_shas if _SHA.match(sha)]
    if reason := _why_a_listing_cannot_be_trusted(repo, remote_url, remote_name, env_override):
        return RemoteHoldings(tuple(_commits_held_here(repo, reported)), listed=False, reason=reason)
    listing = _git(
        repo, *_LOW_SPEED, "ls-remote", "--heads", remote_url, timeout=_LISTING_TIMEOUT_S, env_override=env_override
    )
    if listing is None:
        reason = "could not list the heads of the remote (git ls-remote failed)"
        return RemoteHoldings(tuple(_commits_held_here(repo, reported)), listed=False, reason=reason)
    heads = [line.split("\t", 1)[0] for line in listing.splitlines() if _SHA.match(line.split("\t", 1)[0])]
    return RemoteHoldings(tuple(_commits_held_here(repo, [*heads, *reported])), listed=True)


def main(argv: list[str] | None = None) -> int:
    """``python -m teatree.hooks.remote_holds <remote-url> <remote-name> [<reported-sha> ...]``: one tip per line."""
    args = sys.argv[1:] if argv is None else argv
    try:
        remote_url, remote_name, *reported = args
    except ValueError:
        sys.stderr.write("usage: remote_holds <remote-url> <remote-name> [<reported-sha> ...]\n")
        return 2
    holdings = remote_holdings(Path.cwd(), remote_url=remote_url, remote_name=remote_name, reported_shas=reported)
    if not holdings.listed:
        sys.stderr.write(
            f"⚠ push privacy gate: {holdings.reason} — scanning every commit reachable from the pushed sha that "
            "the reported remote sha does not hold (wider, never narrower).\n"
        )
    sys.stdout.write("".join(f"{tip}\n" for tip in holdings.tips))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
