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

from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_shared_app_server import (
    SharedCodexSession,
    reset_shared_codex_app_servers,
    shared_codex_app_server,
)
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from tests.teatree_agents.test_codex_shared_app_server import _SERVER, FakeCache

_RECORDING_SERVER = (
    "import json, os, sys\nopen(sys.argv[1], 'a').write(json.dumps(dict(os.environ)) + '\\n')\n" + _SERVER
)
_ONE = {"GH_TOKEN": "ghp-one"}
_TWO = {"GH_TOKEN": "ghp-two"}


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cache = FakeCache()
        self.spawned = tmp_path / "spawned.jsonl"
        self.cwd = str(tmp_path)
        self.options = self.options_with({})

    def options_with(self, env: dict[str, str]) -> CodexAppServerOptions:
        return CodexAppServerOptions.from_sdk_options(
            ClaudeAgentOptions(cwd=self.cwd, permission_mode="bypassPermissions", env=env)
        )

    def spawned_tokens(self) -> list[str]:
        return [json.loads(line).get("GH_TOKEN", "") for line in self.spawned.read_text().splitlines()]

    async def open_session(self, env: dict[str, str]) -> SharedCodexSession:
        session = SharedCodexSession(self.options, manager=shared_codex_app_server(self.home, env), resume=None)
        await session.start()
        return session


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    script = tmp_path / "recording_server.py"
    script.write_text(_RECORDING_SERVER)
    state = _World(tmp_path)
    command = (sys.executable, str(script), str(state.spawned))
    with (
        patch("teatree.agents.codex_app_server.codex_command", return_value=command),
        patch("teatree.agents.codex_shared_app_server.CodexAuthCache", lambda _code_home: state.cache),
    ):
        yield state
        reset_shared_codex_app_servers()


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


def test_a_different_token_while_the_server_is_busy_fails_fast_with_a_transport_fallback(world: _World) -> None:
    async def run() -> None:
        busy = await world.open_session(_ONE)
        started = time.monotonic()
        with pytest.raises(HarnessFallbackError, match="busy with another credential") as refusal:
            shared_codex_app_server(world.home, _TWO)
        assert time.monotonic() - started < 2
        assert refusal.value.kind is HarnessFallbackKind.TRANSPORT
        assert shared_codex_app_server(world.home, _ONE) is busy.manager
        await busy.close()

    asyncio.run(run())

    assert world.spawned_tokens() == ["ghp-one"]


def test_neither_the_token_nor_its_fingerprint_is_logged(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    hidden = ["ghp-one", "ghp-two"] + [
        hashlib.sha256(env["GH_TOKEN"].encode()).hexdigest()[:12] for env in (_ONE, _TWO)
    ]
    refusals: list[str] = []

    async def run() -> None:
        busy = await world.open_session(_ONE)
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
