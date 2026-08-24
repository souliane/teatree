"""Tests for :mod:`teatree.loop.scanners.self_update_ci` — the default-branch CI verdict.

The classifier consumes the already-flattened ``check-runs`` list (every page merged
by the shared :mod:`teatree.loop.main_check_runs` reader) and returns a four-way
verdict (green / red / pending / unknown). ``unknown`` is the catch-all for anything
we cannot positively assert green (non-GitHub origin, unresolvable slug, gh failure,
required check absent) — and ``unknown`` is a skip, never a proceed. The ``gh``
shell-out is doubled by the paging ``gh_check_runs`` fake, so the request shape
decides what the verdict can see; the JSON classification runs for real.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

import teatree.loop.scanners.self_update_ci as ci_mod
from teatree.loop.main_check_runs import PAGE_SIZE
from teatree.loop.scanners.pr_sweep import REQUIRED_CHECK_NAME
from teatree.loop.scanners.self_update_ci import CheckRun, CiVerdict, GhMainCiStatus, _classify_check_runs
from tests.teatree_loop.conftest import FakeGhCheckRuns, check_run

StubGh = Callable[..., FakeGhCheckRuns]


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

        GhMainCiStatus(token="secret-pat").verdict(repo=Path("/x"))

        env = fake.env_log[0]
        assert env is not None
        assert env["GH_TOKEN"] == "secret-pat"

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
