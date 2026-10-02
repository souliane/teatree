"""Tests for CronCreate/CronDelete/ScheduleWakeup tracking in the hook router."""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_track_cron_jobs


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


class TestTrackCronJobs:
    def test_cron_create_writes_state_file(self) -> None:
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/12 * * * *", "prompt": "!t3 loop tick"},
                "tool_result": {"id": "job-abc"},
            }
        )

        crons_file = router.STATE_DIR / "s1.crons"
        assert crons_file.is_file()
        state = json.loads(crons_file.read_text(encoding="utf-8"))
        assert "job-abc" in state["jobs"]
        assert state["jobs"]["job-abc"]["name"] == "tick"
        assert state["jobs"]["job-abc"]["cadence"] == 720
        assert state["jobs"]["job-abc"]["cron"] == "*/12 * * * *"

    def test_cron_delete_removes_job(self) -> None:
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/5 * * * *", "prompt": "/followup"},
                "tool_result": {"id": "job-xyz"},
            }
        )
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "CronDelete",
                "tool_input": {"id": "job-xyz"},
            }
        )

        state = json.loads((router.STATE_DIR / "s1.crons").read_text(encoding="utf-8"))
        assert "job-xyz" not in state["jobs"]

    def test_schedule_wakeup_writes_wakeup_state(self) -> None:
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "ScheduleWakeup",
                "tool_input": {"delaySeconds": 270, "reason": "checking build", "prompt": "/foo"},
            }
        )

        state = json.loads((router.STATE_DIR / "s1.crons").read_text(encoding="utf-8"))
        assert state["wakeup"] is not None
        assert state["wakeup"]["name"] == "checking build"
        assert state["wakeup"]["next_epoch"] > 0

    def test_multiple_cron_jobs_coexist(self) -> None:
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/12 * * * *", "prompt": "!t3 loop tick"},
                "tool_result": {"id": "job-1"},
            }
        )
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/5 * * * *", "prompt": "/followup"},
                "tool_result": {"id": "job-2"},
            }
        )

        state = json.loads((router.STATE_DIR / "s1.crons").read_text(encoding="utf-8"))
        assert len(state["jobs"]) == 2
        assert state["jobs"]["job-1"]["name"] == "tick"
        assert state["jobs"]["job-2"]["name"] == "followup"

    def test_ignores_unrelated_tools(self) -> None:
        handle_track_cron_jobs(
            {
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )

        crons_file = router.STATE_DIR / "s1.crons"
        assert not crons_file.is_file()

    def test_ignores_missing_session_id(self) -> None:
        handle_track_cron_jobs(
            {
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/5 * * * *", "prompt": "test"},
                "tool_result": {"id": "job-1"},
            }
        )

        assert not list(router.STATE_DIR.glob("*.crons"))


class TestDeriveLoopName:
    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            # Canonical teatree loop prompt → stable readable name.
            (router._LOOP_PROMPT, "tick"),
            (router._LOOP_PROMPT + " extra trailing words", "tick"),
            ("!t3 loop tick", "tick"),
            # Slash-command forms (leading or embedded token).
            ("/followup", "followup"),
            ("/t3-followup", "t3-followup"),
            ("!/foo", "foo"),
            ("/foo", "foo"),
            ("/loop 5m /babysit-prs", "babysit-prs"),
            # Prose prompt → short label from the first meaningful word,
            # with surrounding punctuation/backticks stripped.
            ("check the deploy status", "check"),
            ("`backtick-word` then more", "backtick-word"),
            ("Deploy.", "Deploy"),
            # Degenerate inputs fall back to a stable label.
            ("!", "loop"),
            ("/", "loop"),
            ("", "loop"),
        ],
    )
    def test_name_derivation(self, prompt: str, expected: str) -> None:
        assert router._derive_loop_name(prompt) == expected

    def test_no_trailing_punctuation_or_backtick(self) -> None:
        name = router._derive_loop_name(router._LOOP_PROMPT)
        assert not name.endswith(".")
        assert "`" not in name

    def test_canonical_prompt_persisted_name_via_handler(self) -> None:
        """Integration: the canonical loop prompt persists a readable job name."""
        handle_track_cron_jobs(
            {
                "session_id": "loop-s",
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/12 * * * *", "prompt": router._LOOP_PROMPT},
                "tool_result": {"id": "job-loop"},
            }
        )

        crons_file = router.STATE_DIR / "loop-s.crons"
        assert crons_file.is_file()
        state = json.loads(crons_file.read_text(encoding="utf-8"))
        assert state["jobs"]["job-loop"]["name"] == "tick"


class TestCronCadenceSeconds:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("*/5 * * * *", 300),
            ("*/12 * * * *", 720),
            ("0 9 * * 1-5", None),
            ("0 * * * *", None),
            ("bad", None),
        ],
    )
    def test_cadence_extraction(self, expr: str, expected: int | None) -> None:
        assert router._cron_cadence_seconds(expr) == expected
