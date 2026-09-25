"""Two live Codex threads share one App Server and one auth-cache writer."""

import asyncio
import os
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from teatree.agents.codex_app_server import CodexAppServerHarness
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_shared_app_server import (
    SharedCodexAppServer,
    SharedCodexSession,
    reset_shared_codex_app_servers,
    shared_codex_app_server,
)
from teatree.agents.harness_registry import HarnessFallbackError
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.mcp.agent_mailbox import MailboxClient

_SERVER = r"""
import json
import sys

thread_count = 0
mailbox_models = {}
experimental_api = False
for raw in sys.stdin:
    request = json.loads(raw)
    method = request['method']
    request_id = request.get('id')
    if request_id is None:
        continue
    if 'runtimeWorkspaceRoots' in request.get('params', {}) and not experimental_api:
        error = {'code': -32600, 'message': f'{method}.runtimeWorkspaceRoots requires experimentalApi capability'}
        print(json.dumps({'id': request_id, 'error': error}), flush=True)
        continue
    if method == 'initialize':
        experimental_api = request['params'].get('capabilities', {}).get('experimentalApi') is True
        response = {}
    elif method == 'account/read':
        response = {'account': {'type': 'chatgpt'}, 'requiresOpenaiAuth': True}
    elif method in {'thread/start', 'thread/resume'}:
        thread_count += 1
        response = {'thread': {'id': request['params'].get('threadId', f'thread-{thread_count}')}}
        token = (
            request['params'].get('config', {}).get('mcp_servers', {})
            .get('teatree', {}).get('env', {}).get('T3_AGENT_MAILBOX_TOKEN')
        )
        if token:
            response['model'] = mailbox_models.setdefault(token, f'mcp-{len(mailbox_models) + 1}')
    elif method == 'turn/start':
        thread_id = request['params']['threadId']
        response = {'turn': {'id': f'turn-{thread_id}'}}
    else:
        response = {}
    sys.stdout.write(json.dumps({'id': request_id, 'result': response}) + '\n')
    if method == 'turn/start':
        sys.stdout.write(json.dumps({'method': 'item/completed', 'params': {
            'threadId': thread_id, 'item': {'id': f'item-{thread_id}', 'type': 'agentMessage', 'text': thread_id}
        }}) + '\n')
        sys.stdout.write(json.dumps({'method': 'turn/completed', 'params': {
            'threadId': thread_id, 'turn': {'id': f'turn-{thread_id}', 'status': 'completed', 'items': []}
        }}) + '\n')
    sys.stdout.flush()
"""


class FakeCache:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.persists = 0
        self.active_persists = 0
        self.max_persists = 0
        self._persist_counter_lock = threading.Lock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Path]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield Path("/unused/auth.json")
        finally:
            self.active -= 1

    def persist(self) -> None:
        with self._persist_counter_lock:
            self.active_persists += 1
            self.max_persists = max(self.max_persists, self.active_persists)
        try:
            time.sleep(0.05)
        finally:
            with self._persist_counter_lock:
                self.active_persists -= 1
                self.persists += 1


def test_shared_manager_reset_replaces_idle_registry_entry(tmp_path: Path) -> None:
    code_home = tmp_path / "private-home"
    first = shared_codex_app_server(code_home)

    reset_shared_codex_app_servers()

    assert first.stopped
    assert shared_codex_app_server(code_home) is not first


def test_two_codex_sessions_share_process_and_auth_writer(tmp_path: Path) -> None:
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
    )

    async def run() -> tuple[list[list[object]], list[str]]:
        sessions = [SharedCodexSession(options, manager=manager, resume=None) for _ in range(2)]
        await asyncio.gather(*(session.start() for session in sessions))
        await asyncio.gather(*(session.query("ping") for session in sessions))
        messages = await asyncio.gather(*(collect(session) for session in sessions))
        await asyncio.gather(*(session.close() for session in sessions))
        return messages, [session.thread_id for session in sessions]

    async def collect(session: SharedCodexSession) -> list[object]:
        return [message async for message in session.receive_response()]

    try:
        messages, thread_ids = asyncio.run(run())
    finally:
        manager.close()

    assert set(thread_ids) == {"thread-1", "thread-2"}
    assert all(
        any(isinstance(message, ResultMessage) and not message.is_error for message in stream) for stream in messages
    )
    assert cache.max_active == 1
    assert cache.active == 0
    assert cache.persists == 2
    assert cache.max_persists == 1


def test_live_codex_threads_keep_distinct_mailbox_config_and_can_exchange(tmp_path: Path) -> None:
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    cache = FakeCache()
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
    )

    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        identities = [broker.register(room="one-ticket", harness="codex", label=f"coder-{i}") for i in range(2)]
        options = [
            CodexAppServerOptions.from_sdk_options(
                ClaudeAgentOptions(
                    cwd=str(tmp_path),
                    permission_mode="bypassPermissions",
                    mcp_servers={"teatree": {"type": "stdio", "command": "t3", "env": identity.mcp_env()}},
                )
            )
            for identity in identities
        ]

        async def run() -> tuple[str, str]:
            sessions = [SharedCodexSession(option, manager=manager, resume=None) for option in options]
            await asyncio.gather(*(session.start() for session in sessions))
            first = MailboxClient(identities[0].socket_path, identities[0].token)
            second = MailboxClient(identities[1].socket_path, identities[1].token)
            await first.send(identities[1].address, "please review", "review-once")
            inbox = await second.inbox()
            assert inbox["messages"][0]["text"] == "please review"
            await asyncio.gather(*(session.close() for session in sessions))
            return sessions[0].model, sessions[1].model

        try:
            models = asyncio.run(run())
        finally:
            manager.close()

    assert set(models) == {"mcp-1", "mcp-2"}


def test_failed_auth_startup_can_be_closed_without_touching_a_closed_loop(tmp_path: Path) -> None:
    class FailingCache:
        @asynccontextmanager
        async def session(self) -> AsyncIterator[Path]:
            msg = "auth unavailable"
            raise RuntimeError(msg)
            yield Path("/unused/auth.json")

        def persist(self) -> None:
            pytest.fail("failed bootstrap must not persist")

    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = SharedCodexAppServer(code_home=tmp_path / "private-home", cache=FailingCache())

    with pytest.raises(RuntimeError, match="auth unavailable"):
        asyncio.run(manager.open_session(options, None))
    manager.close()
    key = (os.getpid(), (tmp_path / "private-home").resolve())
    with patch.dict("teatree.agents.codex_shared_app_server._managers", {key: manager}):
        assert shared_codex_app_server(tmp_path / "private-home") is not manager


def test_dead_codex_process_releases_cache_before_a_replacement(tmp_path: Path) -> None:
    script = tmp_path / "crashing_app_server.py"
    script.write_text(
        _SERVER.replace(
            "    sys.stdout.flush()\n", "    sys.stdout.flush()\n    if method == 'turn/start':\n        break\n"
        )
    )
    cache = FakeCache()
    code_home = tmp_path / "private-home"
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )
    manager = SharedCodexAppServer(
        code_home=code_home,
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
    )

    async def run() -> None:
        session = SharedCodexSession(options, manager=manager, resume=None)
        await session.start()
        await session.query("ping")
        _ = [message async for message in session.receive_response()]
        for _ in range(100):
            if manager.failed:
                break
            await asyncio.sleep(0.01)
        assert manager.failed

    try:
        asyncio.run(run())
        key = (os.getpid(), code_home.resolve())
        with patch.dict("teatree.agents.codex_shared_app_server._managers", {key: manager}):
            assert shared_codex_app_server(code_home) is not manager
        assert cache.active == 0
    finally:
        manager.close()


def test_public_harness_uses_shared_server_and_resumes_same_thread(tmp_path: Path) -> None:
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    code_home = tmp_path / "private-home"
    cache = FakeCache()
    manager = SharedCodexAppServer(
        code_home=code_home,
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
    )
    harness = CodexAppServerHarness(code_home=code_home)

    async def run() -> tuple[str, str]:
        options = ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
        async with harness.open(options) as first:
            await first.query("first")
            first_messages = [message async for message in first.receive_response()]
            first_thread = next(message.session_id for message in first_messages if isinstance(message, ResultMessage))

        options.resume = first_thread
        async with harness.open(options) as second:
            await second.query("second")
            second_messages = [message async for message in second.receive_response()]
            second_thread = next(
                message.session_id for message in second_messages if isinstance(message, ResultMessage)
            )
        return first_thread, second_thread

    key = (os.getpid(), code_home.resolve())
    try:
        with patch.dict("teatree.agents.codex_shared_app_server._managers", {key: manager}):
            assert asyncio.run(run()) == ("thread-1", "thread-1")
        assert cache.persists == 2
    finally:
        manager.close()


def test_crashed_shared_transport_keeps_thread_lineage_in_fallback(tmp_path: Path) -> None:
    script = tmp_path / "crashing_app_server.py"
    script.write_text(
        _SERVER.replace(
            "    if method == 'turn/start':\n        sys.stdout.write",
            "    if method == 'turn/start':\n        sys.stdout.flush()\n        break\n        sys.stdout.write",
        )
    )
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
    )

    async def run() -> None:
        session = SharedCodexSession(options, manager=manager, resume=None)
        await session.start()
        await session.query("ping")
        with pytest.raises(HarnessFallbackError) as failure:
            _ = [message async for message in session.receive_response()]
        assert failure.value.agent_session_id == "thread-1"
        assert failure.value.side_effects_started

    try:
        asyncio.run(run())
    finally:
        manager.close()


def test_idle_shared_server_releases_the_credential_writer(tmp_path: Path) -> None:
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
        idle_seconds=0.01,
    )

    async def run() -> None:
        session = SharedCodexSession(options, manager=manager, resume=None)
        await session.start()
        await session.close()

    try:
        asyncio.run(run())
        for _ in range(100):
            if cache.active == 0 and manager.stopped:
                break
            time.sleep(0.01)
        assert cache.active == 0
        assert manager.retiring
        assert manager.stopped
    finally:
        manager.close()


def test_open_in_flight_prevents_last_close_from_retiring_server(tmp_path: Path) -> None:
    script = tmp_path / "slow_app_server.py"
    script.write_text(
        _SERVER.replace(
            "        thread_count += 1",
            "        import time\n        time.sleep(0.05)\n        thread_count += 1",
        )
    )
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions")
    )
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
        idle_seconds=0.01,
    )

    async def run() -> None:
        first = SharedCodexSession(options, manager=manager, resume=None)
        second = SharedCodexSession(options, manager=manager, resume=None)
        await first.start()
        opening = asyncio.create_task(second.start())
        await asyncio.sleep(0.01)
        closing = asyncio.create_task(first.close())
        await asyncio.gather(opening, closing)
        await asyncio.sleep(0.03)
        assert not manager.retiring
        assert cache.active == 1
        await second.close()

    try:
        asyncio.run(run())
    finally:
        manager.close()


def test_cancelled_close_finishes_persist_before_another_writer(tmp_path: Path) -> None:
    class BlockingCache(FakeCache):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def persist(self) -> None:
            with self._persist_counter_lock:
                self.active_persists += 1
                self.max_persists = max(self.max_persists, self.active_persists)
                first = self.persists == 0
            try:
                if first:
                    self.started.set()
                    assert self.release.wait(timeout=3)
            finally:
                with self._persist_counter_lock:
                    self.active_persists -= 1
                    self.persists += 1

    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    cache = BlockingCache()
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home", cache=cache, command=(sys.executable, str(script)), process_env={}
    )

    async def run() -> None:
        first = SharedCodexSession(options, manager=manager, resume=None)
        second = SharedCodexSession(options, manager=manager, resume=None)
        await asyncio.gather(first.start(), second.start())
        closing = asyncio.create_task(first.close())
        assert await asyncio.to_thread(cache.started.wait, 2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        other = asyncio.create_task(second.close())
        await asyncio.sleep(0.05)
        assert cache.max_persists == 1
        cache.release.set()
        await other

    try:
        asyncio.run(run())
        assert cache.max_persists == 1
    finally:
        cache.release.set()
        manager.close()


def test_failed_persist_still_releases_idle_auth_writer(tmp_path: Path) -> None:
    class FailingPersistCache(FakeCache):
        def persist(self) -> None:
            msg = "credential store temporarily unavailable"
            raise RuntimeError(msg)

    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER)
    cache = FailingPersistCache()
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
        idle_seconds=0.01,
    )

    async def run() -> None:
        session = SharedCodexSession(options, manager=manager, resume=None)
        await session.start()
        with pytest.raises(RuntimeError, match="credential store temporarily unavailable"):
            await session.close()

    try:
        asyncio.run(run())
        for _ in range(100):
            if cache.active == 0 and manager.stopped:
                break
            time.sleep(0.01)
        assert cache.active == 0
        assert manager.stopped
    finally:
        manager.close()


def _wait_until_released(cache: FakeCache, manager: SharedCodexAppServer) -> None:
    for _ in range(300):
        if cache.active == 0 and manager.stopped:
            return
        time.sleep(0.01)


def _assert_replacement_opens(tmp_path: Path, cache: FakeCache, options: CodexAppServerOptions) -> None:
    script = tmp_path / "healthy_app_server.py"
    script.write_text(_SERVER)
    replacement = SharedCodexAppServer(
        code_home=tmp_path / "private-home", cache=cache, command=(sys.executable, str(script)), process_env={}
    )
    try:
        thread_id, _model = asyncio.run(replacement.open_session(options, None))
    finally:
        replacement.close()
    assert thread_id == "thread-1"
    assert cache.max_active == 1


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        pytest.param(
            "        thread_count += 1\n",
            "        sys.stdout.write(json.dumps({'id': request_id, 'error': {'code': 1}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "        continue\n",
            id="rejected",
        ),
        pytest.param(
            "response = {'thread': {'id': request['params'].get('threadId', f'thread-{thread_count}')}}",
            "response = {'thread': {}}",
            id="missing-thread-id",
        ),
    ],
)
def test_failed_thread_open_releases_the_credential_writer(tmp_path: Path, original: str, replacement: str) -> None:
    assert original in _SERVER
    script = tmp_path / "failing_app_server.py"
    script.write_text(_SERVER.replace(original, replacement))
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
        idle_seconds=0.01,
    )

    try:
        with pytest.raises((CodexAppServerError, HarnessFallbackError)):
            asyncio.run(manager.open_session(options, None))
        _wait_until_released(cache, manager)
        assert cache.active == 0
        assert manager.retiring
        assert manager.stopped
    finally:
        manager.close()
    _assert_replacement_opens(tmp_path, cache, options)


def test_cancelled_thread_open_releases_the_credential_writer(tmp_path: Path) -> None:
    received = tmp_path / "thread-start-received"
    script = tmp_path / "slow_app_server.py"
    script.write_text(
        _SERVER.replace(
            "        thread_count += 1\n",
            f"        open({str(received)!r}, 'w').close()\n        import time\n        time.sleep(0.5)\n"
            "        thread_count += 1\n",
        )
    )
    cache = FakeCache()
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = SharedCodexAppServer(
        code_home=tmp_path / "private-home",
        cache=cache,
        command=(sys.executable, str(script)),
        process_env={},
        idle_seconds=0.01,
    )

    async def run() -> None:
        opening = asyncio.create_task(manager.open_session(options, None))
        for _ in range(300):
            if received.exists():
                break
            await asyncio.sleep(0.01)
        assert received.exists()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening

    try:
        asyncio.run(run())
        _wait_until_released(cache, manager)
        assert cache.active == 0
        assert manager.retiring
        assert manager.stopped
    finally:
        manager.close()
    _assert_replacement_opens(tmp_path, cache, options)
