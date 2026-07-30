"""Single tri-state open-PR probe shared by the orphan, teardown, and fast-push gates.

Three gates each asked the forge the same question — "is there an OPEN PR/MR whose
source is this branch?" — with their own hand-rolled ``gh pr list`` / ``glab mr
list`` runner, and they DID NOT agree on the crucial ambiguity: a probe that could
not run (missing CLI, non-zero exit, unparsable JSON) versus one that ran and found
nothing. Collapsing the two is safe for a caller that reacts to both by creating a
PR (fast_push), but it is a safety REGRESSION for a fail-closed reclaim gate, which
must BLOCK on "unknown" and CLEAR on "none".

:class:`PrProbe` answers the question ONCE with an explicit tri-state
(:attr:`PrProbeOutcome.FOUND` / :attr:`~PrProbeOutcome.NONE` /
:attr:`~PrProbeOutcome.UNKNOWN`). Each caller keeps its own posture by mapping the
tri-state through :meth:`PrProbe.url_or_empty` (collapse none+unknown) or
:meth:`PrProbe.url_or_none_on_unknown` (keep them apart, fail closed on unknown) —
the probe implementation no longer forks per caller.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

# Both CLIs list only OPEN PRs/MRs here (``--state open`` / ``--state opened``), one
# row, so a non-empty payload IS an open PR/MR. The branch selector is appended by
# the forge-specific wrapper (``--head`` on GitHub, ``--source-branch`` on GitLab).
_GH_OPEN_PR: tuple[str, ...] = ("gh", "pr", "list", "--state", "open", "--json", "url", "--limit", "1")
_GLAB_OPEN_MR: tuple[str, ...] = ("glab", "mr", "list", "--state", "opened", "--output", "json", "-P", "1")


class PrProbeOutcome(Enum):
    """The three distinguishable answers to "is an OPEN PR/MR backing this branch?"."""

    FOUND = auto()
    NONE = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class PrProbe:
    """The result of one open-PR probe: an outcome plus the URL when one was found."""

    outcome: PrProbeOutcome
    url: str = ""

    @classmethod
    def found(cls, url: str) -> "PrProbe":
        return cls(PrProbeOutcome.FOUND, url)

    @classmethod
    def none(cls) -> "PrProbe":
        return cls(PrProbeOutcome.NONE)

    @classmethod
    def unknown(cls) -> "PrProbe":
        return cls(PrProbeOutcome.UNKNOWN)

    @property
    def is_found(self) -> bool:
        return self.outcome is PrProbeOutcome.FOUND

    @property
    def is_unknown(self) -> bool:
        return self.outcome is PrProbeOutcome.UNKNOWN

    def url_or_empty(self) -> str:
        """FOUND → the URL; NONE and UNKNOWN both → ``""``.

        For callers that treat a failed probe the same as "no PR" because their
        reaction to both is identical — fast_push upserts a PR either way, the
        orphan scan surfaces the branch either way — so the distinction would not
        change what they do.
        """
        return self.url if self.outcome is PrProbeOutcome.FOUND else ""

    def url_or_none_on_unknown(self) -> str | None:
        """FOUND → the URL; NONE → ``""``; UNKNOWN → ``None``.

        For fail-closed callers that must keep "found nothing" (safe to proceed)
        apart from "could not ask" (must refuse) — the open-PR teardown gate reads
        ``None`` as "refuse while it is unknown".
        """
        if self.outcome is PrProbeOutcome.UNKNOWN:
            return None
        return self.url


def find_open_pr_for_branch(repo_dir: str | Path, branch: str) -> PrProbe:
    """The OPEN PR/MR backing *branch* on the repo's forge, as an explicit tri-state.

    The forge is sniffed from ``origin`` via :func:`forge_from_remote`. A repo whose
    ``origin`` is not a recognised forge is :attr:`~PrProbeOutcome.NONE` (no forge,
    therefore no PR to find and nothing to protect), NOT unknown. An empty *branch*
    is :attr:`~PrProbeOutcome.UNKNOWN` — there is nothing to probe, and a
    fail-closed caller must not read that as "no PR". Any CLI failure (missing
    binary, non-zero exit, unparsable JSON) is :attr:`~PrProbeOutcome.UNKNOWN`.
    """
    if not branch:
        return PrProbe.unknown()
    kind = forge_from_remote(git.remote_url(repo=str(repo_dir)))
    if kind == "github":
        return probe_github_open_pr(repo_dir, branch)
    if kind == "gitlab":
        return probe_gitlab_open_pr(repo_dir, branch)
    return PrProbe.none()


def probe_github_open_pr(repo_dir: str | Path, branch: str) -> PrProbe:
    """The tri-state open-PR probe for a known-GitHub repo (``gh pr list --head``)."""
    return _probe_open_pr([*_GH_OPEN_PR, "--head", branch], repo_dir, key="url")


def probe_gitlab_open_pr(repo_dir: str | Path, branch: str) -> PrProbe:
    """The tri-state open-MR probe for a known-GitLab repo (``glab mr list --source-branch``)."""
    return _probe_open_pr([*_GLAB_OPEN_MR, "--source-branch", branch], repo_dir, key="web_url")


def _probe_open_pr(cmd: list[str], repo_dir: str | Path, *, key: str) -> PrProbe:
    """Run *cmd* (a forge CLI that lists only OPEN PRs/MRs) and classify the result.

    A non-empty JSON array is an open PR/MR (:attr:`~PrProbeOutcome.FOUND`, carrying
    the first row's *key*); an empty array is :attr:`~PrProbeOutcome.NONE`. A missing
    binary (``OSError``), a non-zero exit, or unparsable / non-list JSON is
    :attr:`~PrProbeOutcome.UNKNOWN` — the probe could not answer.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, cwd=Path(repo_dir))
    except OSError:
        logger.warning("open-PR probe could not run %r in %s — unknown", cmd[0], repo_dir)
        return PrProbe.unknown()
    if result.returncode != 0:
        logger.warning("open-PR probe %r failed (exit %s) in %s — unknown", cmd[0], result.returncode, repo_dir)
        return PrProbe.unknown()
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return PrProbe.unknown()
    if not isinstance(payload, list):
        return PrProbe.unknown()
    if not payload:
        return PrProbe.none()
    first = payload[0]
    return PrProbe.found(str(first.get(key, "")) if isinstance(first, dict) else "")
