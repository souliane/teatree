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

:class:`GlabMainCiStatus` is the GitLab twin, keyed on the SHA the pull would land
on because GitLab has no per-commit check-run rollup. :class:`ForgeMainCiStatus` is
what the scanner is given: it picks the arm each clone's own ``origin`` speaks, so a
fork on one forge beside a core clone on the other both get a real verdict.

An unresolvable slug, a non-zero ``gh``/``glab`` exit, an offline machine, a
truncated check-runs page, or a commit no gating pipeline ran on all classify as
``unknown`` — and ``unknown`` is a skip, never a proceed. It is also a REFUSAL the
operator must see rather than wait out: unlike red or pending it does not clear on
its own, which is why the scanner notifies on it.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, TypedDict, cast, runtime_checkable

from teatree.forge_credentials import ForgeTokenState, resolve_repo_token
from teatree.loop.main_check_runs import CheckRun, check_runs_argv, parse_check_run_pages
from teatree.loop.scanners.pr_sweep import GREEN_TERMINAL_CONCLUSIONS, REQUIRED_CHECK_NAME
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
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

    The token is resolved from the overlay owning *repo* and exported as
    ``GH_TOKEN``. Ambient CLI auth is never consulted.
    """

    def verdict(self, *, repo: Path) -> CiVerdict:
        slug = _github_slug(repo)
        if not slug:
            return CiVerdict.UNKNOWN
        resolution = resolve_repo_token(str(repo), credential="github_token")
        if resolution.state is not ForgeTokenState.TOKEN:
            return CiVerdict.UNKNOWN
        runs = self._check_runs(slug=slug, branch=_default_branch(repo), token=resolution.token)
        if runs is None:
            return CiVerdict.UNKNOWN
        return _classify_check_runs(runs)

    @staticmethod
    def _check_runs(*, slug: str, branch: str, token: str) -> list[CheckRun] | None:
        import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

        gh = shutil.which("gh") or "gh"
        argv = [gh, *check_runs_argv(slug=slug, ref=branch)]
        env = {"GH_TOKEN": token}
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

    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITLAB_TOKEN", None)
    env.update(extra)
    return env


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


class _GitlabPipeline(TypedDict, total=False):
    """One entry of ``glab api projects/<path>/pipelines``."""

    id: object
    source: object
    status: object


#: Only a pipeline the COMMIT itself triggered answers "may I run this code?". The
#: scheduled eval lanes run on ``ref=main`` too, and reading one of their failures as a
#: red main is a refusal that never clears.
_GATING_PIPELINE_SOURCES = frozenset({"push", "merge_request_event"})

#: A commit rarely carries more than a handful; enough that a re-run never falls off.
_PIPELINE_PAGE_SIZE = 20


class GlabMainCiStatus:
    """``glab``-backed :class:`MainCiStatus` for the commit a pull would land on.

    GitLab has no per-commit check-run rollup, so the unit is the pipeline. The query is
    keyed on ``origin/<default>``'s SHA rather than on the branch name: that SHA is
    exactly what ``pull --ff-only`` is about to make live, and a branch-keyed read would
    answer for whatever landed since.

    ``:fullpath`` is resolved by ``glab`` from *repo*, which is why every call carries
    ``cwd`` — it also spares us URL-encoding a nested group path.
    """

    @staticmethod
    def verdict(*, repo: Path) -> CiVerdict:
        sha = _upstream_sha(repo)
        if not sha:
            return CiVerdict.UNKNOWN
        pipelines = _glab_pipelines(repo=repo, sha=sha)
        if pipelines is None:
            return CiVerdict.UNKNOWN
        return _classify_pipelines(pipelines)


@dataclass(frozen=True, slots=True)
class ForgeMainCiStatus:
    """Route each clone to the verdict source its own ``origin`` speaks.

    The scanner holds ONE status source for a list of clones that need not share a forge
    — a GitHub core clone beside a GitLab fork — so the forge is resolved per repo rather
    than once for the whole pass. An unrecognised origin keeps the fail-closed
    :attr:`CiVerdict.UNKNOWN`.
    """

    github: MainCiStatus = field(default_factory=GhMainCiStatus)
    gitlab: MainCiStatus = field(default_factory=GlabMainCiStatus)

    def verdict(self, *, repo: Path) -> CiVerdict:
        forge = forge_from_remote(git.remote_url(repo=str(repo)))
        if forge == "github":
            return self.github.verdict(repo=repo)
        if forge == "gitlab":
            return self.gitlab.verdict(repo=repo)
        return CiVerdict.UNKNOWN


def _upstream_sha(repo: Path) -> str:
    """The SHA of ``origin/<default>`` — the commit a fast-forward would land on."""
    result = run_allowed_to_fail(
        ["git", "rev-parse", f"origin/{_default_branch(repo)}"],
        cwd=repo,
        expected_codes=None,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _glab_pipelines(*, repo: Path, sha: str) -> list[_GitlabPipeline] | None:
    """Every pipeline GitLab records for *sha*, or ``None`` when the read gave no evidence."""
    import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

    resolution = resolve_repo_token(str(repo), credential="gitlab_token")
    if resolution.state is not ForgeTokenState.TOKEN:
        return None
    glab = shutil.which("glab") or "glab"
    endpoint = f"projects/:fullpath/pipelines?sha={sha}&per_page={_PIPELINE_PAGE_SIZE}"
    try:
        result = run_allowed_to_fail(
            [glab, "api", endpoint],
            cwd=repo,
            expected_codes=None,
            env=_merged_env({"GITLAB_TOKEN": resolution.token}),
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [cast("_GitlabPipeline", entry) for entry in payload if isinstance(entry, dict)]


def _classify_pipelines(pipelines: list[_GitlabPipeline]) -> CiVerdict:
    """Classify the newest GATING pipeline; no gating pipeline is ``unknown``, never green."""
    from teatree.core.merge.ci_rollup import classify_gitlab_pipeline  # noqa: PLC0415 — deferred: pulls the merge stack

    gating = [p for p in pipelines if str(p.get("source") or "") in _GATING_PIPELINE_SOURCES]
    if not gating:
        return CiVerdict.UNKNOWN
    newest = max(gating, key=lambda p: _as_int(p.get("id")))
    return _PIPELINE_VERDICT[classify_gitlab_pipeline(str(newest.get("status") or ""))]


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


_PIPELINE_VERDICT: dict[str, CiVerdict] = {
    "green": CiVerdict.GREEN,
    "pending": CiVerdict.PENDING,
    "failed": CiVerdict.RED,
}


__all__ = ["CiVerdict", "ForgeMainCiStatus", "GhMainCiStatus", "GlabMainCiStatus", "MainCiStatus"]
