"""A catch-up refused at hello time is retried by the serve loop, and the heartbeat waits for it.

The real ``_run_single_overlay`` serve loop runs on the test thread (so the recorder's
writes share the test's DB connection); a fake Socket Mode client greets on connect and
a watcher thread stops the loop once the heartbeat is stamped.
"""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import django.test
import pytest
import slack_sdk.socket_mode
import slack_sdk.web
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.listener_heartbeat import ConnectionLiveness, read_heartbeat
from teatree.backends.slack.receiver import QueuePaths, _run_single_overlay
from teatree.core.models import PendingChatInjection
from teatree.loops import timer_reconciler
from teatree.loops.slack_inbound import catch_up_on_connect
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history

pytestmark = pytest.mark.integration

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}
_OWNER = "U_HUMAN"
_TICK = 0.01

type Handler = Callable[[dict[str, Any], dict[str, str]], dict[str, Any]]


class _GreetingSocket:
    def __init__(self) -> None:
        self.socket_mode_request_listeners: list = []
        self.message_listeners: list = []

    @staticmethod
    def session_id() -> str:
        return "s1"

    @staticmethod
    def is_connected() -> bool:
        return True

    def connect(self) -> None:
        for listener in self.message_listeners:
            listener(self, {"type": "hello"}, '{"type":"hello"}')

    def close(self) -> None:
        pass


def _refuse_first(handler: Handler, on_call: Callable[[], None]) -> Handler:
    calls = 0

    def respond(params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        on_call()
        return {"ok": False, "error": "ratelimited"} if calls == 1 else handler(params, headers)

    return respond


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestARefusedCatchUpIsRetried(django.test.TestCase):
    transport: FakeSlackTransport
    beat: Path
    queues: QueuePaths

    @pytest.fixture(autouse=True)
    def _slack(self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend, tmp_path: Path) -> Iterator[None]:
        _ = overlay_backend
        self.transport = transport
        self.beat = tmp_path / "beat.json"
        self.queues = QueuePaths(events=tmp_path / "events.jsonl", reactions=tmp_path / "reactions.jsonl")
        with (
            patch("teatree.backends.slack.receiver._SERVE_TICK_SECONDS", _TICK),
            patch("teatree.backends.slack.listener_heartbeat.RETRY_BACKOFF_SECONDS", (0.05,)),
            patch.object(slack_sdk.socket_mode, "SocketModeClient", return_value=_GreetingSocket()),
            patch.object(slack_sdk.web, "WebClient", return_value=MagicMock()),
        ):
            yield

    def _serve_until_stamped(self) -> None:
        stop = threading.Event()

        def stop_once_stamped() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and read_heartbeat(self.beat) is None:
                time.sleep(_TICK)
            stop.set()

        watcher = threading.Thread(target=stop_once_stamped, daemon=True)
        watcher.start()
        _run_single_overlay(
            overlay=("demo", "xapp", "xoxb"),
            queues=self.queues,
            stop_event=stop,
            liveness=ConnectionLiveness(
                seed=time.time() - 120, on_connect=catch_up_on_connect, heartbeat_path=self.beat
            ),
        )
        watcher.join(timeout=5.0)

    def _assert_recovered(self, ts: str) -> None:
        assert PendingChatInjection.objects.get().slack_ts == ts
        assert (
            DBTaskResult.objects.filter(
                task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
            ).count()
            == 1
        )
        assert read_heartbeat(self.beat) is not None

    def test_a_failed_first_pass_is_retried_and_recovers_the_dm(self) -> None:
        ts = f"{time.time() - 30:.6f}"
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])
        stamped_before_call: list[bool] = []
        self.transport.handlers["conversations.history"] = _refuse_first(
            self.transport.handlers["conversations.history"], lambda: stamped_before_call.append(self.beat.exists())
        )

        self._serve_until_stamped()

        self._assert_recovered(ts)
        assert stamped_before_call == [False, False]

    def test_an_unresolved_identity_on_the_first_pass_is_retried(self) -> None:
        ts = f"{time.time() - 30:.6f}"
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])
        stamped_before_call: list[bool] = []
        self.transport.handlers["auth.test"] = _refuse_first(
            lambda _params, _headers: {"ok": True, "user_id": "B_BOT"},
            lambda: stamped_before_call.append(self.beat.exists()),
        )

        self._serve_until_stamped()

        self._assert_recovered(ts)
        assert stamped_before_call == [False, False]
