"""Session-end unshipped-work backstop — armed unconditionally, and what it leaves for the next session.

The defect this pins: the backstop only ran when a lifecycle skill happened to be
loaded, and it only ever looked at orphan branches. Whether a session stranded work
is not a function of which skills it loaded.

Nothing a SessionEnd hook prints reaches a model, so the report is left for the next
session in the same checkout (``stranded_work_report``) and the hook prints nothing.
The probes that decide each of the five states are pinned in ``test_session_end_work_probes``.
"""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.session_end_work_check as work_check
from hooks.scripts import stranded_work_report
from tests._session_end_harness import PROJECT as _PROJECT
from tests._session_end_harness import context as _context
from tests._session_end_harness import git as _git
from tests._session_end_harness import repo_with_commit as _repo_with_commit
from tests._session_end_harness import run_end as _run
from tests._session_end_harness import session_end_sandbox


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with session_end_sandbox(tmp_path, monkeypatch):
        yield


class TestArmedUnconditionally:
    """The check must not be gated on which skills the session happened to load."""

    def test_orphan_reported_with_no_lifecycle_skill_loaded(self) -> None:
        orphans = [{"repo": "/ws/backend", "branch": "feat-1", "status": "pushed_orphan", "ahead_count": 3}]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-no-skills"})

        assert "feat-1" in ctx
        assert "/ws/backend" in ctx
        assert "ensure-pr" in ctx

    def test_an_orphan_whose_remote_could_not_be_read_is_not_told_to_push(self) -> None:
        orphans = [{"repo": "/ws/backend", "branch": "feat-3", "status": "remote_unknown", "ahead_count": 2}]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-unreadable"})

        assert "could not be read" in ctx
        assert "t3 teatree pr ensure-pr --repo /ws/backend --branch feat-3" in ctx
        assert "push -u origin feat-3" not in ctx

    def test_orphan_reported_when_only_non_lifecycle_skills_loaded(self) -> None:
        (router.STATE_DIR / "s-other.skills").write_text("ac-python\n", encoding="utf-8")
        orphans = [{"repo": "/ws/backend", "branch": "feat-2", "status": "pushed_orphan", "ahead_count": 1}]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-other"})

        assert "feat-2" in ctx

    def test_the_printed_ensure_pr_command_names_the_repo(self) -> None:
        # Run by hand through the containerized ``t3``, a bare ``.`` is the image WORKDIR.
        orphans = [
            {"repo": "/ws/backend", "branch": "feat-3", "status": "pushed_orphan", "ahead_count": 1},
            {"repo": "/ws/frontend", "branch": "feat-4", "status": "unpushed_orphan", "ahead_count": 2},
        ]
        with patch.object(work_check, "fetch_orphans", return_value=orphans):
            ctx = _context({"session_id": "s-repo-flag"})

        assert "pr ensure-pr --repo /ws/backend --branch feat-3" in ctx
        assert "pr ensure-pr --repo /ws/frontend --branch feat-4" in ctx

    def test_silent_when_nothing_is_stranded(self) -> None:
        assert _run({"session_id": "s-clean"}) == (None, "", "")

    def test_silent_without_a_session_id(self) -> None:
        assert _run({"session_id": ""}) == (None, "", "")

    def test_long_orphan_list_is_previewed(self) -> None:
        many = [
            {"repo": f"/ws/r{i}", "branch": f"br-{i}", "status": "pushed_orphan", "ahead_count": 1} for i in range(8)
        ]
        with patch.object(work_check, "fetch_orphans", return_value=many):
            ctx = _context({"session_id": "s-many"})

        assert ctx.count("[orphan_branch]") == work_check.PREVIEW_LIMIT


class TestNothingIsPrintedAtSessionEnd:
    """Claude Code shows a SessionEnd hook's output only when it fails, so the hook neither prints nor fails."""

    _ORPHAN: ClassVar[list[dict]] = [
        {"repo": "/ws/backend", "branch": "feat-9", "status": "pushed_orphan", "ahead_count": 1}
    ]
    _PR: ClassVar[list[dict]] = [{"number": 7, "title": "Ship the widget"}]

    def test_the_router_ends_the_session_silently_and_leaves_the_report(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.argv", ["hook_router.py", "--event", "SessionEnd"])
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"session_id": "s-close", "cwd": _PROJECT})))

        with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
            router.main()

        assert capsys.readouterr() == ("", "")
        assert "feat-9" in stranded_work_report.claim(_PROJECT).text

    def test_an_end_that_finds_nothing_stranded_drops_the_older_report(self) -> None:
        with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
            _run({"session_id": "s-first", "cwd": _PROJECT})

        _run({"session_id": "s-clean", "cwd": _PROJECT})

        assert stranded_work_report.claim(_PROJECT).text == ""

    @pytest.mark.parametrize(
        ("later_orphans", "put_back"),
        [([], False), (None, True)],
        ids=["found-nothing", "could-not-look"],
    )
    def test_a_report_a_start_holds_is_put_back_only_if_no_later_end_cleared_it(
        self, later_orphans: list[dict] | None, *, put_back: bool
    ) -> None:
        with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
            _run({"session_id": "s-first", "cwd": _PROJECT})
        taken = stranded_work_report.claim(_PROJECT)

        with patch.object(work_check, "fetch_orphans", return_value=later_orphans):
            _run({"session_id": "s-later", "cwd": _PROJECT})
        taken.put_back()

        assert ("feat-9" in stranded_work_report.claim(_PROJECT).text) is put_back

    def test_an_end_that_could_not_answer_carries_the_findings_of_a_report_a_start_holds(self, tmp_path: Path) -> None:
        # The start holds the report while the end looks, and can only put it back once the end has left its own.
        repo = _repo_with_commit(tmp_path)
        with patch.object(work_check, "unpushed_commit_count", return_value=0):
            with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
                _run({"session_id": "s-first", "cwd": str(repo)})
            taken = stranded_work_report.claim(str(repo))
            with (
                patch.object(work_check, "fetch_orphans", return_value=None),
                patch.object(work_check, "read_open_prs", return_value=self._PR),
            ):
                _run({"session_id": "s-no-t3", "cwd": str(repo)})
        taken.put_back()

        report = stranded_work_report.claim(str(repo)).text
        assert "feat-9" in report
        assert "#7 Ship the widget" in report

    def test_an_end_whose_probe_could_not_answer_keeps_the_older_report(self) -> None:
        with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
            _run({"session_id": "s-first", "cwd": _PROJECT})

        with patch.object(work_check, "fetch_orphans", return_value=None):
            _run({"session_id": "s-unanswered", "cwd": _PROJECT})

        assert "feat-9" in stranded_work_report.claim(_PROJECT).text

    def test_an_end_whose_git_probe_timed_out_keeps_the_older_report(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        _run({"session_id": "s-first", "cwd": str(repo)})

        with patch.object(work_check.subprocess, "check_output", side_effect=subprocess.TimeoutExpired("git", 4)):
            _run({"session_id": "s-timed-out", "cwd": str(repo)})

        assert "unpushed" in stranded_work_report.claim(str(repo)).text

    def test_an_end_whose_pr_probe_could_not_answer_keeps_the_older_prs_and_drops_what_the_others_cleared(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_commit(tmp_path)
        with patch.object(work_check, "unpushed_commit_count", return_value=0):
            with (
                patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN),
                patch.object(work_check, "read_open_prs", return_value=self._PR),
            ):
                _run({"session_id": "s-first", "cwd": str(repo)})

            with patch.object(work_check, "read_open_prs", return_value=None):
                _run({"session_id": "s-no-gh", "cwd": str(repo)})

        report = stranded_work_report.claim(str(repo)).text
        assert "#7 Ship the widget" in report
        assert "feat-9" not in report

    def test_an_end_whose_probe_could_not_answer_keeps_its_older_findings_beside_the_new_ones(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_commit(tmp_path)
        with (
            patch.object(work_check, "unpushed_commit_count", return_value=0),
            patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN),
        ):
            _run({"session_id": "s-first", "cwd": str(repo)})

        with patch.object(work_check, "fetch_orphans", return_value=None):
            _run({"session_id": "s-no-t3", "cwd": str(repo)})

        report = stranded_work_report.claim(str(repo)).text
        assert report.count("\n  - [") == 2
        assert "feat-9" in report
        assert "[unpushed]" in report

    @pytest.mark.parametrize(
        ("earlier_age", "later", "carried"),
        [(-60, 0, False), (60, 0, True), (60, 120, False)],
        ids=["aged-out-before-the-end", "carried", "carried-and-aged-out-since"],
    )
    def test_a_finding_carried_by_an_end_that_could_not_answer_keeps_its_age(
        self, tmp_path: Path, earlier_age: int, later: int, *, carried: bool
    ) -> None:
        # Each finding ages alone: the fresh [unpushed] one outlives the carried one beside it.
        repo = _repo_with_commit(tmp_path)
        found_at = time.time() - stranded_work_report.MAX_AGE_SECONDS + earlier_age
        orphan = work_check.WorkItem("orphan_branch", "/ws/backend (feat-9) — 1 commit(s) ahead", "push", found_at)
        stranded_work_report.leave(str(repo), work_check.render_work_report([orphan]))

        with patch.object(work_check, "fetch_orphans", return_value=None):
            _run({"session_id": "s-no-t3", "cwd": str(repo)})

        report = stranded_work_report.claim(str(repo), now=time.time() + later).text
        assert ("feat-9" in report) is carried
        assert "[unpushed]" in report

    def test_a_fresh_finding_is_delivered_past_the_expiry_of_the_older_one_carried_beside_it(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_commit(tmp_path)
        friday = time.time() - stranded_work_report.MAX_AGE_SECONDS + 3600
        with (
            patch.object(stranded_work_report.time, "time", return_value=friday),
            patch.object(work_check, "unpushed_commit_count", return_value=0),
            patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN),
        ):
            _run({"session_id": "s-friday", "cwd": str(repo)})
        os.utime(stranded_work_report._report_path(str(repo)), (friday, friday))
        with patch.object(work_check, "fetch_orphans", return_value=None):
            _run({"session_id": "s-sunday", "cwd": str(repo)})

        monday = stranded_work_report.claim(str(repo), now=time.time() + 2 * 3600).text

        assert "[unpushed]" in monday
        assert "feat-9" not in monday

    def test_an_end_that_could_not_answer_and_found_nothing_else_drops_what_the_others_cleared(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_commit(tmp_path)
        with patch.object(work_check, "unpushed_commit_count", return_value=0):
            with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
                _run({"session_id": "s-first", "cwd": str(repo)})

            with patch.object(work_check, "read_open_prs", return_value=None):
                _run({"session_id": "s-no-gh", "cwd": str(repo)})

        assert stranded_work_report.claim(str(repo)).text == ""

    def test_an_unreadable_index_is_no_answer_and_keeps_the_older_report(self, tmp_path: Path) -> None:
        repo = _repo_with_commit(tmp_path)
        (repo / "b.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "b.txt")
        _run({"session_id": "s-first", "cwd": str(repo)})
        (repo / ".git" / "index").write_bytes(b"not an index")

        assert work_check.scan_work(str(repo)).complete is False
        _run({"session_id": "s-corrupt", "cwd": str(repo)})

        assert "staged" in stranded_work_report.claim(str(repo)).text

    def test_without_a_checkout_nothing_is_left(self) -> None:
        with patch.object(work_check, "fetch_orphans", return_value=self._ORPHAN):
            assert _run({"session_id": "s-nocwd"}) == (None, "", "")

        assert not (router.STATE_DIR / stranded_work_report.REPORT_DIRNAME).exists()


class TestCrashProof:
    def test_a_raising_probe_never_breaks_the_session(self) -> None:
        with patch.object(work_check, "fetch_orphans", side_effect=RuntimeError("boom")):
            assert _run({"session_id": "s-boom"}) == (None, "", "")

    def test_a_nonexistent_cwd_is_ignored(self) -> None:
        assert _run({"session_id": "s-nodir", "cwd": "/definitely/not/a/dir"}) == (None, "", "")
