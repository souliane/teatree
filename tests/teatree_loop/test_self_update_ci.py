"""Tests for :mod:`teatree.loop.scanners.self_update_ci` — the default-branch CI verdict.

The classifier consumes the already-flattened ``check-runs`` list (every page merged
by the shared :mod:`teatree.loop.main_check_runs` reader) and returns a four-way
verdict (green / red / pending / unknown). ``unknown`` is the catch-all for anything
we cannot positively assert green (non-GitHub origin, unresolvable slug, gh failure,
required check absent) — and ``unknown`` is a skip, never a proceed. The ``gh``
shell-out is doubled by the paging ``gh_check_runs`` fake, so the request shape
decides what the verdict can see; the JSON classification runs for real.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import teatree.loop.scanners.self_update_ci as ci_mod
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.main_check_runs import PAGE_SIZE
from teatree.loop.scanners.pr_sweep import REQUIRED_CHECK_NAME
from teatree.loop.scanners.self_update_ci import (
    CheckRun,
    CiVerdict,
    ForgeMainCiStatus,
    GhMainCiStatus,
    GlabMainCiStatus,
    _classify_check_runs,
    _classify_pipelines,
)
from tests.teatree_loop.conftest import FakeGhCheckRuns, check_run

StubGh = Callable[..., FakeGhCheckRuns]


@pytest.fixture(autouse=True)
def _routed_forge_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(_repo: str, *, credential: str) -> ForgeTokenResolution:
        return ForgeTokenResolution(credential, "owner", ForgeTokenState.TOKEN, token=f"routed-{credential}")

    monkeypatch.setattr(ci_mod, "resolve_repo_token", _resolve)


def _runs(*entries: tuple[str, str, str]) -> list[CheckRun]:
    return [{"name": n, "status": s, "conclusion": c} for n, s, c in entries]


class TestClassifyCheckRuns:
    def test_green_when_required_check_succeeds(self) -> None:
        runs = _runs((REQUIRED_CHECK_NAME, "completed", "success"))
        assert _classify_check_runs(runs) is CiVerdict.GREEN

    def test_red_when_required_check_fails(self) -> None:
        runs = _runs((REQUIRED_CHECK_NAME, "completed", "failure"))
        assert _classify_check_runs(runs) is CiVerdict.RED

    def test_pending_when_required_check_not_completed(self) -> None:
        runs = _runs((REQUIRED_CHECK_NAME, "in_progress", ""))
        assert _classify_check_runs(runs) is CiVerdict.PENDING

    def test_pending_wins_over_a_failed_sibling_required_run(self) -> None:
        # A partial run (one required shard still pending, one already failed)
        # reads as pending, never red — the run is not yet conclusive.
        runs = _runs(
            (REQUIRED_CHECK_NAME, "in_progress", ""),
            (REQUIRED_CHECK_NAME, "completed", "failure"),
        )
        assert _classify_check_runs(runs) is CiVerdict.PENDING

    def test_unknown_when_required_check_absent(self) -> None:
        runs = _runs(("lint", "completed", "success"))
        assert _classify_check_runs(runs) is CiVerdict.UNKNOWN

    def test_unknown_on_empty_runs(self) -> None:
        assert _classify_check_runs([]) is CiVerdict.UNKNOWN

    def test_neutral_and_skipped_conclusions_count_as_green(self) -> None:
        for conclusion in ("neutral", "skipped"):
            runs = _runs((REQUIRED_CHECK_NAME, "completed", conclusion))
            assert _classify_check_runs(runs) is CiVerdict.GREEN


def _on_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the clone's ``origin`` at a resolvable GitHub slug on ``main``."""
    monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
    monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")
    monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")


class TestGhMainCiStatusVerdict:
    def test_empty_route_never_inherits_ambient_gh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_github(monkeypatch)
        calls: list[object] = []
        monkeypatch.setenv("GH_TOKEN", "ambient-token")
        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(
            ci_mod,
            "resolve_repo_token",
            lambda *_a, **_k: ForgeTokenResolution("github_token", "owner", ForgeTokenState.UNSET),
        )

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN
        assert calls == []

    def test_unknown_for_non_github_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "git@gitlab.com:x/y.git")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_for_unresolvable_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_when_gh_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh) -> None:
        _on_github(monkeypatch)
        gh_check_runs(ci_mod, returncode=1)

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_green_classified_from_gh_payload(self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh) -> None:
        _on_github(monkeypatch)
        fake = gh_check_runs(ci_mod, runs=[check_run(REQUIRED_CHECK_NAME)])

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN
        assert any("check-runs" in part for part in fake.argv_log[0])

    def test_reads_check_runs_across_all_pages(self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh) -> None:
        """Regression (#4090 sibling): an unpaginated read sees only page 1.

        The invoked argv must request EVERY page — otherwise a required check
        landing past GitHub's own 30-run default page silently classifies as
        absent (``unknown``) even when main is genuinely green.
        """
        _on_github(monkeypatch)
        fake = gh_check_runs(ci_mod, runs=[check_run(REQUIRED_CHECK_NAME)])

        GhMainCiStatus().verdict(repo=Path("/x"))

        argv = fake.argv_log[0]
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert "--jq" not in argv, "gh refuses --slurp together with --jq"

    def test_required_check_past_the_first_page_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh
    ) -> None:
        """The concrete failure mode: the required check is the last of more runs than a page holds.

        The fake serves page 1 alone to an argv that does not ask for the rest,
        so ``pages_served`` is the truncation an unpaginated read suffers — and a
        truncated read classifies a green ``main`` as ``unknown``, which is a skip.
        """
        _on_github(monkeypatch)
        runs = [check_run(f"job-{i}") for i in range(PAGE_SIZE)] + [check_run(REQUIRED_CHECK_NAME)]
        fake = gh_check_runs(ci_mod, runs=runs)

        verdict = GhMainCiStatus().verdict(repo=Path("/x"))

        assert fake.pages_served == 2, "the read stopped at page 1 — the required check was never seen"
        assert fake.runs_served == len(runs)
        assert verdict is CiVerdict.GREEN

    def test_unknown_on_unparsable_gh_output(self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh) -> None:
        _on_github(monkeypatch)
        gh_check_runs(ci_mod, raw_stdout="not json")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_token_exported_as_gh_token_env(self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh) -> None:
        _on_github(monkeypatch)
        fake = gh_check_runs(ci_mod, runs=[check_run(REQUIRED_CHECK_NAME)])

        GhMainCiStatus().verdict(repo=Path("/x"))

        env = fake.env_log[0]
        assert env is not None
        assert env["GH_TOKEN"] == "routed-github_token"

    def test_unknown_when_gh_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_github(monkeypatch)

        def _raise(*_a: object, **_k: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", _raise)

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_default_branch_falls_back_to_main_on_error(
        self, monkeypatch: pytest.MonkeyPatch, gh_check_runs: StubGh
    ) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")

        def _raise(**_k: object) -> str:
            raise RuntimeError

        monkeypatch.setattr(ci_mod.git, "default_branch", _raise)
        fake = gh_check_runs(ci_mod, runs=[check_run(REQUIRED_CHECK_NAME)])

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN
        assert any("/commits/main/" in part for part in fake.argv_log[0])


def _fake_glab(monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0) -> list[list[str]]:
    """Double the ``glab`` shell-out; the returned list records every argv issued."""
    issued: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        issued.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(ci_mod, "run_allowed_to_fail", _run)
    return issued


def _pipeline(pipeline_id: int, source: str, status: str) -> dict[str, object]:
    return {"id": pipeline_id, "source": source, "status": status}


class TestClassifyPipelines:
    def test_green_when_the_commit_push_pipeline_succeeded(self) -> None:
        assert _classify_pipelines([_pipeline(2, "push", "success")]) is CiVerdict.GREEN

    def test_red_when_the_commit_push_pipeline_failed(self) -> None:
        assert _classify_pipelines([_pipeline(2, "push", "failed")]) is CiVerdict.RED

    def test_pending_while_the_pipeline_runs(self) -> None:
        assert _classify_pipelines([_pipeline(2, "push", "running")]) is CiVerdict.PENDING

    def test_manual_and_skipped_are_pending_not_green(self) -> None:
        # Inherited from the shared classifier: a blocked or never-run pipeline is
        # not evidence the required stages passed.
        for status in ("manual", "skipped"):
            assert _classify_pipelines([_pipeline(2, "push", status)]) is CiVerdict.PENDING

    def test_newest_gating_pipeline_wins(self) -> None:
        older, newer = _pipeline(1, "push", "failed"), _pipeline(9, "push", "success")
        assert _classify_pipelines([older, newer]) is CiVerdict.GREEN

    def test_scheduled_pipelines_are_not_the_commit_gate(self) -> None:
        # The nightly eval lanes run on ref=main. Reading one of their failures as a
        # red main is a refusal that never clears — the very shape this module fixes.
        assert _classify_pipelines([_pipeline(9, "schedule", "failed")]) is CiVerdict.UNKNOWN

    def test_unknown_when_no_pipeline_ran_on_the_commit(self) -> None:
        assert _classify_pipelines([]) is CiVerdict.UNKNOWN


def _on_gitlab(monkeypatch: pytest.MonkeyPatch, *, upstream_sha: str = "abc123") -> None:
    monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "git@gitlab.com:ns/group/repo.git")
    monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")
    monkeypatch.setattr(ci_mod, "_upstream_sha", lambda _repo: upstream_sha)


class TestGlabMainCiStatusVerdict:
    def test_empty_route_never_inherits_ambient_glab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch)
        calls: list[object] = []
        monkeypatch.setenv("GITLAB_TOKEN", "ambient-token")
        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(
            ci_mod,
            "resolve_repo_token",
            lambda *_a, **_k: ForgeTokenResolution("gitlab_token", "owner", ForgeTokenState.UNSET),
        )

        assert GlabMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN
        assert calls == []

    def test_green_from_the_commits_own_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch)
        _fake_glab(monkeypatch, stdout=json.dumps([_pipeline(3, "push", "success")]))

        assert GlabMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN

    def test_query_is_keyed_on_the_sha_the_pull_would_land_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch, upstream_sha="deadbeef")
        issued = _fake_glab(monkeypatch, stdout="[]")

        GlabMainCiStatus().verdict(repo=Path("/x"))

        assert "sha=deadbeef" in issued[0][-1]
        assert "projects/:fullpath/pipelines" in issued[0][-1]

    def test_unknown_when_the_upstream_sha_is_unresolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch, upstream_sha="")

        assert GlabMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_when_glab_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch)
        _fake_glab(monkeypatch, stdout="", returncode=1)

        assert GlabMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_on_an_unparsable_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_gitlab(monkeypatch)
        _fake_glab(monkeypatch, stdout="not json")

        assert GlabMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN


class TestForgeMainCiStatusRouting:
    def test_a_gitlab_origin_gets_a_real_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression: before per-forge routing this repo's own origin resolved to
        # no GitHub slug, so every tick was a fail-closed UNKNOWN and the clone never
        # advanced. Revert the routing and this assertion goes red.
        _on_gitlab(monkeypatch)
        _fake_glab(monkeypatch, stdout=json.dumps([_pipeline(3, "push", "success")]))

        assert ForgeMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN

    def test_a_github_origin_still_reads_check_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        gh_check_runs: StubGh,
    ) -> None:
        _on_github(monkeypatch)
        gh_check_runs(ci_mod, runs=[check_run(REQUIRED_CHECK_NAME)])

        assert ForgeMainCiStatus(github=GhMainCiStatus()).verdict(repo=Path("/x")) is CiVerdict.GREEN

    def test_unknown_for_an_unrecognised_forge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "git@example.invalid:x/y.git")

        assert ForgeMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN
