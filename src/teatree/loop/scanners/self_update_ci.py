"""Default-branch CI verdict for the self-update scanner's fail-closed gate.

Before the self-update scanner applies a fast-forward pull it asks: is the
default branch's CI actually green? A ff-pull onto a red default branch
drags broken code into the running orchestrator, so the scanner only
proceeds on an *explicit* green and skips on anything else (fail closed).

:class:`MainCiStatus` is the injectable Protocol the scanner depends on;
:class:`GhMainCiStatus` is the ``gh``-backed production implementation. It
mirrors :meth:`teatree.loop.scanners.pr_sweep.GhPrApiClient.main_check_failed`
— the same ``gh api repos/{slug}/commits/<default>/check-runs`` call and
the same :data:`teatree.loop.scanners.pr_sweep.GREEN_TERMINAL_CONCLUSIONS`
classification — but resolves the ``owner/repo`` slug from the clone's own
``origin`` remote and returns a four-way verdict instead of a bool so the
scanner can distinguish red from pending from "cannot tell".

A non-GitHub origin, an unresolvable slug, a non-zero ``gh`` exit, or an
offline machine all classify as ``unknown`` — and ``unknown`` is a skip,
never a proceed.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

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
    :class:`teatree.loop.scanners.pr_sweep.GhPrApiClient` does.
    """

    def __init__(self, *, token: str = "") -> None:
        self.token = token

    def verdict(self, *, repo: Path) -> CiVerdict:
        slug = _github_slug(repo)
        if not slug:
            return CiVerdict.UNKNOWN
        rc, out = self._check_runs(slug=slug, branch=_default_branch(repo))
        if rc != 0:
            return CiVerdict.UNKNOWN
        return _classify_check_runs(out)

    def _check_runs(self, *, slug: str, branch: str) -> tuple[int, str]:
        import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

        gh = shutil.which("gh") or "gh"
        argv = [gh, "api", f"repos/{slug}/commits/{branch}/check-runs", "--jq", _CHECK_RUNS_JQ]
        env = {"GH_TOKEN": self.token} if self.token else None
        try:
            result = run_allowed_to_fail(argv, expected_codes=None, env=_merged_env(env))
        except FileNotFoundError:
            return 127, ""
        return result.returncode, result.stdout


_CHECK_RUNS_JQ = "[.check_runs[] | {name: .name, status: .status, conclusion: .conclusion}]"


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


def _classify_check_runs(out: str) -> CiVerdict:
    """Classify the default branch's ``check-runs`` payload.

    The required check is ``test (3.13)`` — the same gate the PR sweep
    enforces. When it is absent the verdict is ``unknown`` (we cannot
    assert green without seeing the required check). When present: a non-
    completed status is ``pending``, a non-green conclusion is ``red``,
    and a green conclusion is ``green``. A still-pending required check
    wins over an already-failed sibling so a partial run is never read as
    red.
    """
    runs = _parse_runs(out)
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


def _parse_runs(out: str) -> list[dict[str, str]]:
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _run_verdict(run: dict[str, str]) -> CiVerdict:
    status = str(run.get("status") or "").upper()
    if status and status != "COMPLETED":
        return CiVerdict.PENDING
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion in _GREEN_CONCLUSIONS:
        return CiVerdict.GREEN
    return CiVerdict.RED


__all__ = ["CiVerdict", "GhMainCiStatus", "MainCiStatus"]
