"""The stale-heartbeat inbox job: catch up only while the overlay's listener is not live.

A real ``SlackBotBackend`` over the fake Slack transport; the window each pass reads is
observed on the ``conversations.history`` call it makes.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.loops.slack_catchup_job import CatchUpState, SlackCatchUpJob
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport

_DAY = 86400.0
_MARGIN_AND_ROOT_LOOKBACK = 60.0 + _DAY


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeSlackTransport:
    fake = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", fake.post)
    monkeypatch.setattr(slack_http.httpx, "get", fake.get)
    return fake


@pytest.fixture
def heartbeat(tmp_path: Path) -> Iterator[Path]:
    with (
        patch("teatree.backends.slack.listener_heartbeat.DATA_DIR", tmp_path),
        patch("teatree.paths.DATA_DIR", tmp_path),
    ):
        yield tmp_path / "slack-listener-heartbeat.json"


@pytest.fixture(autouse=True)
def _slack_overlays(transport: FakeSlackTransport) -> Iterator[None]:
    _ = transport
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
    with patch("teatree.loops.slack_catchup_job.messaging_from_overlay", return_value=backend):
        yield


def _stamp(path: Path, overlays: dict[str, float]) -> None:
    payload = {"updated_at": max(overlays.values()), "interval_seconds": 15, "overlays": overlays}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _job(overlay: str, recorded: list[RawAPIDict] | None = None) -> SlackCatchUpJob:
    sink = recorded if recorded is not None else []

    def record(_overlay: str, event: dict) -> bool:
        sink.append(event)
        return True

    return SlackCatchUpJob(overlay=overlay, record=record, wake=MagicMock())


def _window_oldests(transport: FakeSlackTransport) -> list[float]:
    return [float(call.payload["oldest"]) for call in transport.calls_to("conversations.history")]


def test_runs_only_for_the_stale_overlay(transport: FakeSlackTransport, heartbeat: Path) -> None:
    now = time.time()
    _stamp(heartbeat, {"fresh": now, "dead": now - 600})

    assert _job("fresh").scan() == []
    assert transport.calls_to("conversations.history") == []

    _job("dead").scan()
    [oldest] = _window_oldests(transport)
    assert oldest == pytest.approx(now - 600 - _MARGIN_AND_ROOT_LOOKBACK, abs=5)


def test_absent_heartbeat_walks_the_full_window_once_then_the_memo_bounds_it(
    transport: FakeSlackTransport, heartbeat: Path
) -> None:
    assert not heartbeat.exists()

    _job("demo").scan()
    _job("demo").scan()

    oldests = _window_oldests(transport)
    assert oldests[0] == pytest.approx(time.time() - 7 * _DAY - _MARGIN_AND_ROOT_LOOKBACK, abs=5)
    assert oldests[-1] == pytest.approx(time.time() - _MARGIN_AND_ROOT_LOOKBACK, abs=5)
    assert len(oldests) > 2


def test_the_second_stale_pass_starts_later_than_the_first(transport: FakeSlackTransport, heartbeat: Path) -> None:
    _stamp(heartbeat, {"demo": time.time() - 600})

    _job("demo").scan()
    _job("demo").scan()

    first, second = _window_oldests(transport)
    assert second > first + 500


def test_completed_slices_are_saved_before_the_job_budget_stops(transport: FakeSlackTransport, heartbeat: Path) -> None:
    _ = heartbeat
    with patch("teatree.loops.slack_catchup_job.JOB_BUDGET_SECONDS", 0):
        _job("demo").scan()

    state = CatchUpState.load("demo")
    assert state.caught_up_at == pytest.approx(time.time() - 6 * _DAY, abs=120)
    assert len(_window_oldests(transport)) == 1

    _job("demo").scan()
    assert _window_oldests(transport)[1] > _window_oldests(transport)[0] + _DAY - 120


def test_finished_slice_survives_a_later_refused_slice(transport: FakeSlackTransport, heartbeat: Path) -> None:
    _ = heartbeat
    reads = 0

    def history(_params: dict, _headers: dict) -> dict:
        nonlocal reads
        reads += 1
        return {"ok": True, "messages": []} if reads == 1 else {"ok": False, "error": "access_denied"}

    transport.handlers["conversations.history"] = history
    with pytest.raises(SlackReadRefusedError):
        _job("demo").scan()

    assert reads == 2
    assert CatchUpState.load("demo").caught_up_at == pytest.approx(time.time() - 6 * _DAY, abs=120)


def test_state_save_flushes_and_replaces_atomically(heartbeat: Path) -> None:
    _ = heartbeat
    state = CatchUpState(caught_up_at=1000.0)
    with patch("teatree.loops.slack_catchup_job.os.fsync", wraps=os.fsync) as sync:
        state.save("demo")
    sync.assert_called_once()

    path = CatchUpState.path("demo")
    before = path.read_text(encoding="utf-8")

    def partial_dump(_data: dict, stream: TextIO) -> None:
        stream.write("{")
        message = "interrupted"
        raise RuntimeError(message)

    with (
        patch("teatree.loops.slack_catchup_job.json.dump", side_effect=partial_dump),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        CatchUpState(caught_up_at=2000.0).save("demo")

    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_state_save_reports_an_unwritable_destination(heartbeat: Path, tmp_path: Path) -> None:
    _ = heartbeat
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    with (
        patch.object(CatchUpState, "path", return_value=blocker / "state.json"),
        pytest.raises(FileExistsError, match="File exists"),
    ):
        CatchUpState(caught_up_at=1000.0).save("demo")


def test_checkpoint_loads_in_a_second_interpreter(heartbeat: Path, tmp_path: Path) -> None:
    _ = heartbeat
    state = CatchUpState(caught_up_at=1000.0)
    state.seen.update({"message:D:1.0": 1.0})
    state.save("demo")
    code = (
        "from pathlib import Path; import os, sys; "
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.django_settings'; import django; django.setup(); "
        "import teatree.paths as paths; paths.DATA_DIR = Path(sys.argv[1]); "
        "from teatree.loops.slack_catchup_job import CatchUpState; "
        "s = CatchUpState.load('demo'); print(s.caught_up_at, s.seen.as_mapping()['message:D:1.0'])"
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1000.0 1.0"


def test_unreadable_state_is_warned_and_restarts_from_empty(heartbeat: Path, caplog: pytest.LogCaptureFixture) -> None:
    _ = heartbeat
    state_file = CatchUpState.path("demo")
    state_file.parent.mkdir(parents=True)
    state_file.write_text("not json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        state = CatchUpState.load("demo")

    assert state.caught_up_at is None
    assert state.seen.as_mapping() == {}
    assert "unreadable" in caplog.text


def test_a_refused_pass_does_not_advance_the_window(transport: FakeSlackTransport, heartbeat: Path) -> None:
    stamp = time.time() - 600
    _stamp(heartbeat, {"demo": stamp})
    transport.default_responses["conversations.history"] = {"ok": False, "error": "access_denied"}

    with pytest.raises(SlackReadRefusedError):
        _job("demo").scan()
    transport.default_responses["conversations.history"] = {"ok": True, "messages": []}
    _job("demo").scan()

    refused, retried = _window_oldests(transport)
    assert retried == pytest.approx(refused, abs=5)


def test_a_dm_in_the_window_reaches_the_recorder(transport: FakeSlackTransport, heartbeat: Path) -> None:
    _stamp(heartbeat, {"demo": time.time() - 600})
    ts = f"{time.time() - 30:.6f}"
    transport.default_responses["conversations.history"] = {
        "ok": True,
        "messages": [{"ts": ts, "user": "U_HUMAN", "text": "ping"}],
    }
    recorded: list[RawAPIDict] = []

    _job("demo", recorded).scan()

    assert [(e["ts"], e["channel"], e["channel_type"]) for e in recorded] == [(ts, "D-USER", "im")]


def test_an_overlay_without_a_slack_backend_refuses_catch_up(transport: FakeSlackTransport, heartbeat: Path) -> None:
    _ = heartbeat
    with (
        patch("teatree.loops.slack_catchup_job.messaging_from_overlay", return_value=None),
        pytest.raises(RuntimeError, match="Slack backend"),
    ):
        _job("demo").scan()

    assert transport.calls == []
