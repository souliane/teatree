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

:func:`forge_cli_env` is the second half of that unification: a forge READ
authenticates through the same credential chain the writer path already resolves
(souliane/teatree#4116). A probe shelled without it comes back UNKNOWN for every
private repo, and a caller reading that as "no PR" refuses the second push to a
branch whose PR already exists. It serves the GitHub arm only: the GitLab arm shells
out to nothing, because the deploy IMAGE declares no ``glab`` and a probe must not
depend on a binary only an operator's host bind mount happens to supply.
"""

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from teatree.forge_credentials import ForgeTokenState, resolve_repo_token, resolve_url_token
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_run import run_with_status
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

# ``gh`` lists only OPEN PRs here, one row, so a non-empty payload IS an open PR. The
# branch selector (``--head``) is appended by the forge-specific wrapper. The GitLab arm
# runs no CLI at all — the image declares no ``glab``, so that side reads over the
# token-authenticated HTTP API instead (see :func:`probe_gitlab_open_pr`).
_GH_OPEN_PR: tuple[str, ...] = ("gh", "pr", "list", "--state", "open", "--json", "url", "--limit", "1")


def forge_cli_env(repo: str | Path = ".") -> dict[str, str] | None:
    """Return a GitHub-only environment with the owning overlay's routed token."""
    resolution = resolve_repo_token(str(repo), credential="github_token")
    if resolution.state is not ForgeTokenState.TOKEN:
        return None
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITLAB_TOKEN", None)
    env.pop("GLAB_CONFIG_DIR", None)
    env["GH_TOKEN"] = resolution.token
    return env


def gitlab_cli_env(repo: str | Path = ".") -> dict[str, str] | None:
    """Return a GitLab-only environment with the owning overlay's routed token."""
    resolution = resolve_repo_token(str(repo), credential="gitlab_token")
    if resolution.state is not ForgeTokenState.TOKEN:
        return None
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GLAB_CONFIG_DIR", None)
    env["GITLAB_TOKEN"] = resolution.token
    return env


def forge_url_cli_env(url: str) -> dict[str, str] | None:
    """Return an explicit GitHub env bound to the overlay owning *url*."""
    resolution = resolve_url_token(url, credential="github_token")
    if resolution.state is not ForgeTokenState.TOKEN:
        return None
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env["GH_TOKEN"] = resolution.token
    return env


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

    The remote read itself must tell "unreadable repo" apart from "readable repo,
    unrecognised host": ``git remote get-url`` failing (not a git repo, no
    ``origin``, a corrupted ``.git``) is :attr:`~PrProbeOutcome.UNKNOWN` — there is
    a PR to protect and no way to ask — never :attr:`~PrProbeOutcome.NONE`, which
    would tell a fail-closed teardown gate "nothing to protect" on a repo it
    simply could not read.
    """
    if not branch:
        return PrProbe.unknown()
    kind = forge_for_repo(repo_dir)
    if kind is None:
        return PrProbe.unknown()
    if kind == "github":
        return probe_github_open_pr(repo_dir, branch)
    if kind == "gitlab":
        return probe_gitlab_open_pr(repo_dir, branch)
    return PrProbe.none()


def forge_for_repo(repo_dir: str | Path) -> str | None:
    """Return the forge owning ``origin``; ``None`` means the remote was unreadable."""
    remote = run_with_status(repo=str(repo_dir), args=["remote", "get-url", "origin"])
    if remote.returncode != 0:
        return None
    return forge_from_remote(remote.stdout.strip())


def probe_github_open_pr(repo_dir: str | Path, branch: str) -> PrProbe:
    """The tri-state open-PR probe for a known-GitHub repo (``gh pr list --head``)."""
    return _probe_open_pr([*_GH_OPEN_PR, "--head", branch], repo_dir, key="url")


def probe_gitlab_open_pr(repo_dir: str | Path, branch: str) -> PrProbe:
    """The tri-state open-MR probe for a known-GitLab repo, over the HTTP API (#151).

    Shells out to nothing, because the deploy IMAGE declares no ``glab``
    (``deploy/Dockerfile``): a role may only shell out to a tool the image carries.
    Where the operator's host happens to bind-mount one — ``~/.local/bin`` is such a
    mount, and a ``glab`` found there IS executable and authenticated — the old
    ``glab mr list --source-branch`` worked by accident of that host's contents, and
    the same probe answered UNKNOWN on any box without it. So the CLI's presence was
    never the thing to reason from; an undeclared dependency was. Over HTTP this arm
    answers the same everywhere, on the token the overlay picks for that remote.

    Any failure below is UNKNOWN, including one raised while BUILDING the backend: this
    runs inside the git pre-push hook, where an exception would abort the push itself.
    """
    try:
        url = _gitlab_open_mr_url(repo_dir, branch)
    except Exception:  # noqa: BLE001 — the probe runs in a pre-push hook and must never raise into it.
        logger.warning("open-MR probe could not build a GitLab backend for %s — unknown", repo_dir)
        return PrProbe.unknown()
    if url is None:
        return PrProbe.unknown()
    return PrProbe.found(url) if url else PrProbe.none()


def _gitlab_open_mr_url(repo_dir: str | Path, branch: str) -> str | None:
    """The repo's own code host answering ``fetch_open_pr_url_for_branch``, or ``None``.

    Reached through :func:`~teatree.core.backend_factory.code_host_for_repo_from_overlay`
    — the sanctioned core↔backends bridge — so ``teatree.core`` never names a concrete
    forge backend (``tach`` enforces that boundary) AND the read authenticates with the
    token the overlay picks FOR THIS REMOTE. That second property matters: a repo whose
    MRs are authored under a scoped bot credential is then probed with the same
    credential that would create the MR, rather than whatever ambient token happened to
    be in the environment.

    ``None`` (no host configured) is UNKNOWN, never "no PR" — a fail-closed caller must
    not read an unresolvable credential as verified absence. The import is deferred
    because ``backend_factory`` pulls Django in at import time and this probe runs
    inside the git pre-push hook.
    """
    from teatree.core.backend_factory import code_host_for_repo_from_overlay  # noqa: PLC0415 — deferred: Django (#151)

    host = code_host_for_repo_from_overlay(str(repo_dir))
    if host is None:
        return None
    return host.fetch_open_pr_url_for_branch(repo=str(repo_dir), branch=branch)


def _probe_open_pr(cmd: list[str], repo_dir: str | Path, *, key: str) -> PrProbe:
    """Run *cmd* (a forge CLI that lists only OPEN PRs/MRs) and classify the result.

    A non-empty JSON array whose first row carries a non-empty string at *key* is an
    open PR/MR (:attr:`~PrProbeOutcome.FOUND`, carrying that url); an empty array is
    :attr:`~PrProbeOutcome.NONE`. A missing binary (``OSError``), a non-zero exit,
    unparsable / non-list JSON, or a row shaped otherwise is
    :attr:`~PrProbeOutcome.UNKNOWN` — the probe could not answer. Both CLIs are
    invoked with an explicit field selector, so a row missing *key* is a changed
    output schema, never a PR with no url: reporting FOUND-with-``""`` there let the
    fail-closed teardown adapter read an unverified open PR as verified absence.
    """
    stdout = _open_pr_stdout(cmd, repo_dir)
    if stdout is None:
        return PrProbe.unknown()
    return _classify_open_pr_rows(stdout, cmd[0], key=key)


def _open_pr_stdout(cmd: list[str], repo_dir: str | Path) -> str | None:
    """*cmd*'s stdout, or ``None`` when the forge CLI could not answer at all."""
    env = forge_cli_env(repo_dir)
    if env is None:
        logger.warning("open-PR probe has no routed GitHub token for %s — unknown", repo_dir)
        return None
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, cwd=Path(repo_dir), env=env)
    except OSError:
        logger.warning("open-PR probe could not run %r in %s — unknown", cmd[0], repo_dir)
        return None
    if result.returncode != 0:
        logger.warning("open-PR probe %r failed (exit %s) in %s — unknown", cmd[0], result.returncode, repo_dir)
        return None
    return result.stdout or "[]"


def _classify_open_pr_rows(stdout: str, tool: str, *, key: str) -> PrProbe:
    """Classify a forge CLI's OPEN-PR listing into the tri-state probe result."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return PrProbe.unknown()
    if not isinstance(payload, list):
        return PrProbe.unknown()
    if not payload:
        return PrProbe.none()
    first = payload[0]
    url = first.get(key) if isinstance(first, dict) else None
    if not isinstance(url, str) or not url:
        logger.warning("open-PR probe %r returned a row with no %r — unknown", tool, key)
        return PrProbe.unknown()
    return PrProbe.found(url)
