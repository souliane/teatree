"""Tests for :mod:`teatree.loop.scanners.self_update_ci` — the default-branch CI verdict.

The classifier consumes the ``gh api .../check-runs`` payload and returns a
four-way verdict (green / red / pending / unknown). ``unknown`` is the
catch-all for anything we cannot positively assert green (non-GitHub origin,
unresolvable slug, gh failure, required check absent) — and ``unknown`` is a
skip, never a proceed. Only the ``gh`` shell-out is stubbed; the JSON
classification runs for real.
"""

import json
from pathlib import Path

import pytest

import teatree.loop.scanners.self_update_ci as ci_mod
from teatree.loop.scanners.pr_sweep import REQUIRED_CHECK_NAME
from teatree.loop.scanners.self_update_ci import CiVerdict, GhMainCiStatus, _classify_check_runs


def _runs(*entries: tuple[str, str, str]) -> str:
    return json.dumps([{"name": n, "status": s, "conclusion": c} for n, s, c in entries])


class TestClassifyCheckRuns:
    def test_green_when_required_check_succeeds(self) -> None:
        out = _runs((REQUIRED_CHECK_NAME, "completed", "success"))
        assert _classify_check_runs(out) is CiVerdict.GREEN

    def test_red_when_required_check_fails(self) -> None:
        out = _runs((REQUIRED_CHECK_NAME, "completed", "failure"))
        assert _classify_check_runs(out) is CiVerdict.RED

    def test_pending_when_required_check_not_completed(self) -> None:
        out = _runs((REQUIRED_CHECK_NAME, "in_progress", ""))
        assert _classify_check_runs(out) is CiVerdict.PENDING

    def test_pending_wins_over_a_failed_sibling_required_run(self) -> None:
        # A partial run (one required shard still pending, one already failed)
        # reads as pending, never red — the run is not yet conclusive.
        out = _runs(
            (REQUIRED_CHECK_NAME, "in_progress", ""),
            (REQUIRED_CHECK_NAME, "completed", "failure"),
        )
        assert _classify_check_runs(out) is CiVerdict.PENDING

    def test_unknown_when_required_check_absent(self) -> None:
        out = _runs(("lint", "completed", "success"))
        assert _classify_check_runs(out) is CiVerdict.UNKNOWN

    def test_unknown_on_empty_payload(self) -> None:
        assert _classify_check_runs("[]") is CiVerdict.UNKNOWN
        assert _classify_check_runs("") is CiVerdict.UNKNOWN

    def test_unknown_on_invalid_json(self) -> None:
        assert _classify_check_runs("not json") is CiVerdict.UNKNOWN

    def test_unknown_when_payload_is_not_a_list(self) -> None:
        assert _classify_check_runs('{"check_runs": []}') is CiVerdict.UNKNOWN

    def test_neutral_and_skipped_conclusions_count_as_green(self) -> None:
        for conclusion in ("neutral", "skipped"):
            out = _runs((REQUIRED_CHECK_NAME, "completed", conclusion))
            assert _classify_check_runs(out) is CiVerdict.GREEN


class TestGhMainCiStatusVerdict:
    def _stub_gh(self, monkeypatch: pytest.MonkeyPatch, *, rc: int, out: str) -> list[list[str]]:
        calls: list[list[str]] = []

        class _Proc:
            returncode = rc
            stdout = out
            stderr = ""

        def _run(argv: list[str], **_kw: object) -> _Proc:
            calls.append(argv)
            return _Proc()

        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", _run)
        return calls

    def test_unknown_for_non_github_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "git@gitlab.com:x/y.git")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_for_unresolvable_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_unknown_when_gh_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")
        monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")
        self._stub_gh(monkeypatch, rc=1, out="")

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_green_classified_from_gh_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")
        monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")
        calls = self._stub_gh(monkeypatch, rc=0, out=_runs((REQUIRED_CHECK_NAME, "completed", "success")))

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN
        assert any("check-runs" in part for part in calls[0])

    def test_token_exported_as_gh_token_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")
        monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")
        captured: list[dict[str, str] | None] = []

        class _Proc:
            returncode = 0
            stdout = _runs((REQUIRED_CHECK_NAME, "completed", "success"))
            stderr = ""

        def _run(argv: list[str], *, env: dict[str, str] | None = None, **_kw: object) -> _Proc:
            captured.append(env)
            return _Proc()

        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", _run)

        GhMainCiStatus(token="secret-pat").verdict(repo=Path("/x"))

        assert captured[0] is not None
        assert captured[0]["GH_TOKEN"] == "secret-pat"

    def test_unknown_when_gh_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")
        monkeypatch.setattr(ci_mod.git, "default_branch", lambda **_k: "main")

        def _raise(*_a: object, **_k: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr(ci_mod, "run_allowed_to_fail", _raise)

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.UNKNOWN

    def test_default_branch_falls_back_to_main_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_mod.git, "remote_url", lambda **_k: "https://github.com/o/r")
        monkeypatch.setattr(ci_mod.git, "remote_slug", lambda **_k: "o/r")

        def _raise(**_k: object) -> str:
            raise RuntimeError

        monkeypatch.setattr(ci_mod.git, "default_branch", _raise)
        calls = self._stub_gh(monkeypatch, rc=0, out=_runs((REQUIRED_CHECK_NAME, "completed", "success")))

        assert GhMainCiStatus().verdict(repo=Path("/x")) is CiVerdict.GREEN
        assert any("/commits/main/" in part for part in calls[0])
