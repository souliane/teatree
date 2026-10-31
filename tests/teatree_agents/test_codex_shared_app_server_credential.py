"""A rotated forge token retires the idle shared Codex server; a busy one answers a fast named fallback."""

import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_shared_app_server import (
    SharedCodexSession,
    reset_shared_codex_app_servers,
    shared_codex_app_server,
)
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.live_control import RejectCode, SteerRejectedError
from tests.teatree_agents.test_codex_shared_app_server import _SERVER, FakeCache


def _edited(server: str, old: str, new: str) -> str:
    assert old in server
    return server.replace(old, new, 1)


_RECORDING_SERVER = _edited(
    "import json, os, sys\nopen(sys.argv[1], 'a').write(json.dumps(dict(os.environ)) + '\\n')\n" + _SERVER,
    "    method = request['method']\n",
    "    method = request['method']\n"
    "    open(sys.argv[2], 'a').write(json.dumps([method, request.get('params', {}).get('threadId')]) + '\\n')\n",
)
_TURN_STAYS_OPEN = _edited(
    _RECORDING_SERVER,
    "    if method == 'turn/start':\n        sys.stdout.write(json.dumps({'method': 'item/completed'",
    "    if False:\n        sys.stdout.write(json.dumps({'method': 'item/completed'",
)
_REFUSES_TURN_START = _edited(
    _RECORDING_SERVER,
    "    if method == 'initialize':",
    "    if method == 'turn/start':\n"
    "        print(json.dumps({'id': request_id, 'error': {'code': -32000, 'message': 'refused'}}), flush=True)\n"
    "        continue\n"
    "    if method == 'initialize':",
)
_ONE = {"GH_TOKEN": "ghp-one"}
_TWO = {"GH_TOKEN": "ghp-two"}


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cache = FakeCache()
        self.spawned = tmp_path / "spawned.jsonl"
        self.requests = tmp_path / "requests.jsonl"
        self.cwd = str(tmp_path)
        self.options = self.options_with({})

    def options_with(self, env: dict[str, str]) -> CodexAppServerOptions:
        return CodexAppServerOptions.from_sdk_options(
            ClaudeAgentOptions(cwd=self.cwd, permission_mode="bypassPermissions", env=env)
        )

    def turn_starts(self) -> list[str | None]:
        logged = [json.loads(line) for line in self.requests.read_text().splitlines()]
        return [thread_id for method, thread_id in logged if method == "turn/start"]

    def spawned_tokens(self) -> list[str]:
        return [json.loads(line).get("GH_TOKEN", "") for line in self.spawned.read_text().splitlines()]

    async def open_session(self, env: dict[str, str]) -> SharedCodexSession:
        session = SharedCodexSession(self.options, manager=shared_codex_app_server(self.home, env), resume=None)
        await session.start()
        return session


def _serving(tmp_path: Path, server: str) -> Iterator[_World]:
    script = tmp_path / "recording_server.py"
    script.write_text(server)
    state = _World(tmp_path)
    command = (sys.executable, str(script), str(state.spawned), str(state.requests))
    with (
        patch("teatree.agents.codex_app_server.codex_command", return_value=command),
        patch("teatree.agents.codex_shared_app_server.CodexAuthCache", lambda _code_home: state.cache),
    ):
        yield state
        reset_shared_codex_app_servers()


@pytest.fixture
def world(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_World]:
    yield from _serving(tmp_path, getattr(request, "param", _RECORDING_SERVER))


turn_stays_open = pytest.mark.parametrize("world", [_TURN_STAYS_OPEN], indirect=True, ids=["turn_stays_open"])
refuses_turn_start = pytest.mark.parametrize("world", [_REFUSES_TURN_START], indirect=True, ids=["refuses_turn_start"])


async def _run_one_turn(session: SharedCodexSession) -> None:
    await session.query("go")
    async for _ in session.receive_response():
        pass


def test_the_same_token_reuses_the_live_server_whatever_the_pytest_cap(world: _World) -> None:
    first = shared_codex_app_server(world.home, _ONE)

    assert shared_codex_app_server(world.home, {**_ONE, "PYTEST_XDIST_AUTO_NUM_WORKERS": "9"}) is first


def test_a_rotated_token_retires_the_idle_server_and_starts_one_new_server_on_the_same_home(world: _World) -> None:
    async def run() -> tuple[object, object]:
        first = await world.open_session(_ONE)
        first_manager = first.manager
        await first.close()
        second = await world.open_session(_TWO)
        second_manager = second.manager
        await second.close()
        return first_manager, second_manager

    first_manager, second_manager = asyncio.run(run())

    assert first_manager is not second_manager
    assert world.cache.max_active == 1
    assert world.spawned_tokens() == ["ghp-one", "ghp-two"]


@turn_stays_open
def test_a_different_token_while_a_turn_is_running_fails_fast_with_a_transport_fallback(world: _World) -> None:
    async def run() -> None:
        busy = await world.open_session(_ONE)
        await busy.query("go")
        started = time.monotonic()
        with pytest.raises(HarnessFallbackError, match="busy with another credential") as refusal:
            shared_codex_app_server(world.home, _TWO)
        assert time.monotonic() - started < 2
        assert refusal.value.kind is HarnessFallbackKind.TRANSPORT
        assert shared_codex_app_server(world.home, _ONE) is busy.manager
        await busy.close()

    asyncio.run(run())

    assert world.spawned_tokens() == ["ghp-one"]


@turn_stays_open
def test_neither_the_token_nor_its_fingerprint_is_logged(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    hidden = ["ghp-one", "ghp-two"] + [
        hashlib.sha256(env["GH_TOKEN"].encode()).hexdigest()[:12] for env in (_ONE, _TWO)
    ]
    refusals: list[str] = []

    async def run() -> None:
        busy = await world.open_session(_ONE)
        await busy.query("go")
        with pytest.raises(HarnessFallbackError) as refusal:
            shared_codex_app_server(world.home, _TWO)
        refusals.append(str(refusal.value))
        await busy.close()
        await (await world.open_session(_TWO)).close()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())

    visible = " ".join([caplog.text, *refusals])
    assert not [secret for secret in hidden if secret in visible]


def test_a_session_opened_on_a_retiring_server_restarts_it_with_its_own_token(world: _World) -> None:
    async def run() -> None:
        first = shared_codex_app_server(world.home, _ONE)
        stale = SharedCodexSession(world.options_with(_ONE), manager=first, resume=None)
        await stale.start()
        await stale.close()
        first._retiring.set()
        session = SharedCodexSession(world.options_with(_TWO), manager=first, resume=None)
        await session.start()
        await session.close()

    asyncio.run(run())

    assert world.spawned_tokens() == ["ghp-one", "ghp-two"]


def test_a_thread_held_idle_after_its_turn_does_not_block_a_rotated_token_and_its_follow_up_reads_session_closed(
    world: _World,
) -> None:
    async def run() -> tuple[object, object]:
        held = await world.open_session(_ONE)
        await _run_one_turn(held)
        replacement = shared_codex_app_server(world.home, _TWO)
        with pytest.raises(SteerRejectedError) as follow_up:
            await held.follow_up("anything new?")
        assert follow_up.value.code is RejectCode.SESSION_CLOSED
        await held.close()
        return held.manager, replacement

    retired, replacement = asyncio.run(run())

    assert replacement is not retired
    assert world.spawned_tokens() == ["ghp-one"]


def test_a_thread_held_idle_still_takes_a_follow_up_turn_on_the_same_server_for_the_same_token(world: _World) -> None:
    async def run() -> None:
        held = await world.open_session(_ONE)
        await _run_one_turn(held)
        assert shared_codex_app_server(world.home, _ONE) is held.manager
        await held.follow_up("one more")
        async for _ in held.receive_response():
            pass
        await held.close()

    asyncio.run(run())

    assert len(set(world.turn_starts())) == 1
    assert len(world.turn_starts()) == 2
    assert world.spawned_tokens() == ["ghp-one"]


@turn_stays_open
def test_closing_a_session_mid_turn_releases_the_server_for_a_rotated_token(world: _World) -> None:
    async def run() -> object:
        running = await world.open_session(_ONE)
        await running.query("go")
        await running.close()
        return shared_codex_app_server(world.home, _TWO)

    assert asyncio.run(run()) is not None


@refuses_turn_start
def test_a_turn_start_the_server_refused_never_marks_the_server_busy(world: _World) -> None:
    async def run() -> None:
        session = await world.open_session(_ONE)
        with pytest.raises(CodexAppServerError):
            await session.query("go")
        shared_codex_app_server(world.home, _TWO)
        await session.close()

    asyncio.run(run())
