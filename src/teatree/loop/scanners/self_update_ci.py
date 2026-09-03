"""Default-branch CI verdict for the self-update scanner's fail-closed gate.

Before the self-update scanner applies a fast-forward pull it asks: is the
default branch's CI actually green? A ff-pull onto a red default branch
drags broken code into the running orchestrator, so the scanner only
proceeds on an *explicit* green and skips on anything else (fail closed).

:class:`MainCiStatus` is the injectable Protocol the scanner depends on;
:class:`GhMainCiStatus` is the ``gh``-backed production implementation. It reads
the same ``commits/<default>/check-runs`` endpoint as
:meth:`teatree.loop.scanners.pr_sweep_adapters.GhPrApiClient.main_check_failed`,
through the shared :mod:`teatree.loop.main_check_runs` paginated reader (#4090
sibling — an unpaginated read of that endpoint sees only the first 30 check-runs,
so a required check landing past page 1 reads as absent), and the same
:data:`teatree.loop.scanners.pr_sweep.GREEN_TERMINAL_CONCLUSIONS` classification
— but resolves the ``owner/repo`` slug from the clone's own ``origin`` remote and
returns a four-way verdict instead of a bool so the scanner can distinguish red
from pending from "cannot tell".

A non-GitHub origin, an unresolvable slug, a non-zero ``gh`` exit, an offline
machine, or a truncated/unreadable check-runs page all classify as ``unknown`` —
and ``unknown`` is a skip, never a proceed.
"""

from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from teatree.loop.main_check_runs import CheckRun, check_runs_argv, parse_check_run_pages
from teatree.loop.scanners.pr_sweep import GREEN_TERMINAL_CONCLUSIONS, REQUIRED_CHECK_NAME
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail


class CiVerdict(Enum):
    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    UNKNOWN = "unknown"


@runtime_checkable
class MainCiStatus(Protocol):
    def verdict(self, *, repo: Path) -> CiVerdict: ...  # pragma: no branch


_GREEN_CONCLUSIONS = {c.lower() for c in GREEN_TERMINAL_CONCLUSIONS}


class GhMainCiStatus:
    """``gh``-backed :class:`MainCiStatus` for the clone's default branch.

    *token* — when non-empty — is exported as ``GH_TOKEN`` so a private
    overlay repo can be queried under that overlay's PAT, exactly as
    :class:`teatree.loop.scanners.pr_sweep_adapters.GhPrApiClient` does.
    """

    def __init__(self, *, token: str = "") -> None:
        self.token = token

    def verdict(self, *, repo: Path) -> CiVerdict:
        slug = _github_slug(repo)
        if not slug:
            return CiVerdict.UNKNOWN
        runs = self._check_runs(slug=slug, branch=_default_branch(repo))
        if runs is None:
            return CiVerdict.UNKNOWN
        return _classify_check_runs(runs)

    def _check_runs(self, *, slug: str, branch: str) -> list[CheckRun] | None:
        import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

        gh = shutil.which("gh") or "gh"
        argv = [gh, *check_runs_argv(slug=slug, ref=branch)]
        env = {"GH_TOKEN": self.token} if self.token else None
        try:
            result = run_allowed_to_fail(argv, expected_codes=None, env=_merged_env(env))
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        return parse_check_run_pages(result.stdout)


def _github_slug(repo: Path) -> str:
    """Resolve the ``owner/repo`` slug, or ``""`` for a non-GitHub origin."""
    url = git.remote_url(repo=str(repo))
    if "github.com" not in url:
        return ""
    return git.remote_slug(repo=str(repo))


def _default_branch(repo: Path) -> str:
    try:
        return git.default_branch(repo=str(repo))
    except RuntimeError:
        return "main"


def _merged_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    if extra is None:
        return None
    import os  # noqa: PLC0415 — deferred: loaded only on this code path

    return {**os.environ, **extra}


def _classify_check_runs(runs: list[CheckRun]) -> CiVerdict:
    """Classify the default branch's already-flattened check-runs.

    The required check is ``test (3.13)`` — the same gate the PR sweep
    enforces. When it is absent the verdict is ``unknown`` (we cannot
    assert green without seeing the required check — including when it is
    absent only because the read was truncated, which is why *runs* must
    already carry every page). When present: a non-completed status is
    ``pending``, a non-green conclusion is ``red``, and a green conclusion
    is ``green``. A still-pending required check wins over an already-failed
    sibling so a partial run is never read as red.
    """
    if not runs:
        return CiVerdict.UNKNOWN
    required = [r for r in runs if r.get("name") == REQUIRED_CHECK_NAME]
    if not required:
        return CiVerdict.UNKNOWN
    verdicts = {_run_verdict(r) for r in required}
    if CiVerdict.PENDING in verdicts:
        return CiVerdict.PENDING
    if CiVerdict.RED in verdicts:
        return CiVerdict.RED
    return CiVerdict.GREEN


def _run_verdict(run: CheckRun) -> CiVerdict:
    status = str(run.get("status") or "").upper()
    if status and status != "COMPLETED":
        return CiVerdict.PENDING
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion in _GREEN_CONCLUSIONS:
        return CiVerdict.GREEN
    return CiVerdict.RED


__all__ = ["CiVerdict", "GhMainCiStatus", "MainCiStatus"]
