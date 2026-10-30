import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from teatree.cli.doctor import checks_bot_push_events, run_checks
from teatree.config.cold_reader import SettingRead
from teatree.utils.run import CompletedProcess


@pytest.mark.parametrize(("actor", "expected"), [("build-bot", 1), ("person", 0)])
def test_doctor_flags_a_human_push_to_a_bot_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor: str,
    expected: int,
) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.setattr(checks_bot_push_events, "_collect_repos", lambda: [("work", repo)])
    monkeypatch.setattr(
        checks_bot_push_events.git,
        "remote_url",
        lambda repo: "https://github.com/build-bot/engine.git",
    )
    monkeypatch.setattr(
        checks_bot_push_events.bot_push_gate.cold_reader,
        "read_setting_confirmed",
        lambda key: SettingRead({"github.com": ["build-bot"]}, readable=True),
    )
    event = {
        "id": "123",
        "type": "PushEvent",
        "actor": {"login": actor},
        "created_at": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(checks_bot_push_events, "_read_events", lambda remote, path: [event])

    assert int(checks_bot_push_events.check_bot_push_events()) == expected
    output = capsys.readouterr().out
    if expected:
        assert "person" not in output
    else:
        assert "person" in output


def test_doctor_does_not_report_clean_when_forge_events_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.setattr(checks_bot_push_events, "_collect_repos", lambda: [("work", repo)])
    monkeypatch.setattr(
        checks_bot_push_events.git, "remote_url", lambda repo: "https://github.com/build-bot/engine.git"
    )
    monkeypatch.setattr(
        checks_bot_push_events.bot_push_gate.cold_reader,
        "read_setting_confirmed",
        lambda key: SettingRead({"github.com": ["build-bot"]}, readable=True),
    )

    def unreadable(remote: checks_bot_push_events.BotRepo, path: Path) -> list[dict[str, object]]:
        message = "forge unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(checks_bot_push_events, "_read_events", unreadable)

    assert not checks_bot_push_events.check_bot_push_events()
    assert "could not be read" in capsys.readouterr().out


def test_doctor_runner_calls_the_push_event_probe() -> None:
    assert "check_bot_push_events()" in inspect.getsource(run_checks.run_doctor_checks)


def test_gitlab_event_read_uses_the_routed_bot_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.setattr(checks_bot_push_events, "_collect_repos", lambda: [("work", repo)])
    monkeypatch.setattr(
        checks_bot_push_events.git,
        "remote_url",
        lambda repo: "https://gitlab.com/build-bot/engine.git",
    )
    monkeypatch.setattr(
        checks_bot_push_events.bot_push_gate.cold_reader,
        "read_setting_confirmed",
        lambda key: SettingRead({"gitlab.com": ["build-bot"]}, readable=True),
    )
    monkeypatch.setattr(checks_bot_push_events, "forge_write_env", lambda path: {"GITLAB_TOKEN": "bot-key"})
    commands: list[list[str]] = []

    def api(cmd: list[str], **kwargs: object) -> CompletedProcess[str]:
        commands.append(cmd)
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["GITLAB_TOKEN"] == "bot-key"
        event = {"id": "123", "action_name": "pushed", "author": {"username": "person"}, "push_data": {}}
        return CompletedProcess(cmd, 0, json.dumps([event]), "")

    monkeypatch.setattr(checks_bot_push_events, "run_allowed_to_fail", api)

    assert not checks_bot_push_events.check_bot_push_events()
    assert commands == [
        [
            "glab",
            "api",
            "projects/build-bot%2Fengine/events?action=pushed&per_page=100&page=1",
            "--hostname",
            "gitlab.com",
        ]
    ]
