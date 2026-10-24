"""The executor's Codex App Server: its own lease and login, a fresh home per server, a probe per thread."""

import asyncio
import fcntl
import json
import os
import signal
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

from teatree.agents import codex_read_only_server
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY
from teatree.agents.codex_read_only_executor import EXECUTOR_LOGIN_ENTRY
from teatree.agents.codex_read_only_server import ExecutorCodexAppServer, executor_session
from teatree.agents.codex_read_only_verdict import EXECUTOR_HOME_ROOT, SCRATCH_ROOT
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.forge_credentials import ROUTED_GH_KEY_ENV
from teatree.utils import run, secrets
from tests.teatree_agents._executor_fakes import (
    EXAMPLE_ROOT,
    EXECUTOR_ACCOUNT,
    EXECUTOR_ID,
    ROTATED_LOGIN,
    WORKER_LOGIN,
    FakeExecutor,
    attested,
)

pytestmark = pytest.mark.usefixtures("pass_store")

_PLANNING = ClaudeAgentOptions(
    cwd=EXAMPLE_ROOT, permission_mode="bypassPermissions", disallowed_tools=["Write", "Edit", "NotebookEdit"]
)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeExecutor]:
    executor = FakeExecutor(tmp_path)
    for key, value in executor.install().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("T3_CODEX_HOME", str(tmp_path / "worker-codex-home"))
    # The fake `pass` first on PATH is the store boundary here, so `pass` may run.
    monkeypatch.setattr(secrets, "run_bounded_group", run.run_bounded_group)
    with executor.running():
        yield executor
        codex_read_only_server.reset_executor_app_servers()


async def _turn(options: ClaudeAgentOptions = _PLANNING) -> str:
    async with executor_session(options, "planning") as session:
        await session.query("plan it")
        messages = [message async for message in session.receive_response()]
    return "".join(
        block.text
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, TextBlock)
    )


def _app_server_execs(fake: FakeExecutor) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["exec", "-i"] and "setsid" in call]


def _sweeps(fake: FakeExecutor) -> list[int]:
    return [index for index, call in enumerate(fake.calls) if call[:1] == ["exec"] and "home_root" in call[-1]]


def _codex_home(call: list[str]) -> str:
    return next(item.partition("=")[2] for item in call if item.startswith("CODEX_HOME="))


def _first_script_call(fake: FakeExecutor, script: str) -> int:
    return next(index for index, call in enumerate(fake.calls) if script in call)


def test_a_planning_turn_runs_unsandboxed_inside_the_executor(fake: FakeExecutor) -> None:
    assert asyncio.run(_turn()) == "OK"

    (launch,) = _app_server_execs(fake)
    assert launch[launch.index("setsid") - 1] == EXECUTOR_ID
    assert _codex_home(launch).startswith(f"{EXECUTOR_HOME_ROOT}/codex-")
    (thread_start,) = fake.thread_starts
    assert (thread_start["sandbox"], thread_start["approvalPolicy"]) == ("danger-full-access", "untrusted")
    assert thread_start["cwd"] == EXAMPLE_ROOT
    assert thread_start["config"]["projects"] == {EXAMPLE_ROOT: {"trust_level": "untrusted"}}


def test_executor_home_is_fresh_per_server_start(fake: FakeExecutor) -> None:
    asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()
    asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    first, second = (_codex_home(call) for call in _app_server_execs(fake))
    assert first != second
    assert fake.homes() == []


def test_a_home_is_created_owner_only_after_a_clean_sweep_and_an_emptied_tmpfs(fake: FakeExecutor) -> None:
    asyncio.run(_turn())

    (launch,) = _app_server_execs(fake)
    sweep, *_ = _sweeps(fake)
    steps = [
        _first_script_call(fake, codex_read_only_server.RESET_SCRIPT),
        _first_script_call(fake, codex_read_only_server.CREATE_HOME_SCRIPT),
        _first_script_call(fake, codex_read_only_server.WRITE_AUTH_SCRIPT),
    ]
    assert sweep < steps[0] < steps[1] < steps[2] < fake.calls.index(launch)
    home = Path(f"{fake.fs}{_codex_home(launch)}")
    assert [stat.S_IMODE(path.stat().st_mode) for path in (home, home / "auth.json")] == [0o700, 0o600]


def test_the_tmpfs_is_empty_when_a_server_starts_and_after_it_stops(fake: FakeExecutor) -> None:
    Path(f"{fake.fs}{SCRATCH_ROOT}/left-by-an-earlier-session").write_text("x", encoding="utf-8")
    Path(f"{fake.fs}{EXECUTOR_HOME_ROOT}/notes").write_text("x", encoding="utf-8")

    asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    assert fake.scratch_at_turns == [[]]
    assert (fake.tmpfs(SCRATCH_ROOT), fake.tmpfs(EXECUTOR_HOME_ROOT)) == ([], [])


def test_both_sweeps_run_under_the_executor_lease(fake: FakeExecutor) -> None:
    asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    assert fake.sweep_leases == ["leased", "leased"]


def test_executor_store_persists_a_rotated_login(fake: FakeExecutor) -> None:
    fake.rotated_login = ROTATED_LOGIN
    fake.install()

    asyncio.run(_turn())

    assert fake.stored(EXECUTOR_LOGIN_ENTRY)["last_refresh"] == "2026-10-12T09:00:00Z"
    assert len(fake.pass_writes) == 2
    assert fake.pass_writes[0].startswith(f"{EXECUTOR_LOGIN_ENTRY}.bak-")
    assert fake.pass_writes[1] == EXECUTOR_LOGIN_ENTRY


def test_home_is_kept_when_persist_fails(fake: FakeExecutor) -> None:
    fake.rotated_login = ROTATED_LOGIN
    fake.install()
    Path(f"{fake.pass_store}.readonly").touch()

    with pytest.raises(HarnessFallbackError):
        asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    (home,) = fake.homes()
    assert json.loads((home / "auth.json").read_text(encoding="utf-8"))["last_refresh"] == "2026-10-12T09:00:00Z"


def _stale_home(
    fake: FakeExecutor, name: str, last_refresh: str, account: str = EXECUTOR_ACCOUNT, refresh_token: str | None = None
) -> Path:
    home = Path(f"{fake.fs}{EXECUTOR_HOME_ROOT}/codex-{name}")
    home.mkdir(parents=True)
    tokens = {"account_id": account, "refresh_token": name if refresh_token is None else refresh_token}
    login = {"tokens": tokens, "last_refresh": last_refresh}
    (home / "auth.json").write_text(json.dumps(login), encoding="utf-8")
    return home


def _refresh_token(fake: FakeExecutor) -> object:
    tokens = fake.stored(EXECUTOR_LOGIN_ENTRY)["tokens"]
    assert isinstance(tokens, dict)
    return tokens["refresh_token"]


def test_newer_login_in_a_stale_home_is_recovered(fake: FakeExecutor) -> None:
    newer = _stale_home(fake, "newer", "2026-10-11T00:00:00Z")
    older = _stale_home(fake, "older", "2026-09-01T00:00:00Z")

    asyncio.run(_turn())

    assert _refresh_token(fake) == "newer"
    assert not newer.exists()
    assert not older.exists()


def test_a_stray_server_is_ended_before_its_newer_login_is_recovered_and_the_keeper_survives(
    fake: FakeExecutor,
) -> None:
    stray = fake.stray("stray")
    home = _stale_home(fake, "stray", "2026-10-11T00:00:00Z")

    asyncio.run(_turn())

    assert stray.wait(timeout=10) == -signal.SIGKILL
    assert fake.keeper_alive
    assert _refresh_token(fake) == "stray"
    assert not home.exists()


def test_the_sweep_runs_before_the_launch_and_after_the_stop(fake: FakeExecutor) -> None:
    asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    (launch,) = _app_server_execs(fake)
    start, stop = _sweeps(fake)
    last_read = max(i for i, call in enumerate(fake.calls) if codex_read_only_server.READ_AUTH_SCRIPT in call)
    assert start < fake.calls.index(launch) < stop < last_read
    assert fake.keeper_alive


@pytest.mark.parametrize("answer", [{"survivors": [4242]}, {"survivors": [], "extra": 1}, "not json"])
def test_an_unclean_sweep_starts_no_server_and_keeps_stale_homes(fake: FakeExecutor, answer: object) -> None:
    fake.raw_sweep = answer
    fake.install()
    home = _stale_home(fake, "kept", "2026-10-11T00:00:00Z")

    with pytest.raises(HarnessFallbackError) as refused:
        asyncio.run(_turn())

    assert refused.value.kind is HarnessFallbackKind.ACCESS
    assert _app_server_execs(fake) == []
    assert fake.homes() == [home]
    assert _refresh_token(fake) == "stored"


def test_a_tmpfs_entry_the_reset_cannot_remove_refuses_the_run(fake: FakeExecutor) -> None:
    stuck = Path(f"{fake.fs}{EXECUTOR_HOME_ROOT}/stuck")
    (stuck / "file").parent.mkdir()
    (stuck / "file").write_text("x", encoding="utf-8")
    stuck.chmod(0o500)
    try:
        with pytest.raises(HarnessFallbackError, match="the executor tmpfs could not be emptied") as refused:
            asyncio.run(_turn())
    finally:
        stuck.chmod(0o700)

    assert refused.value.kind is HarnessFallbackKind.ACCESS
    assert _app_server_execs(fake) == []


def test_a_stale_home_that_cannot_be_read_is_kept_and_refuses_the_run(fake: FakeExecutor) -> None:
    home = Path(f"{fake.fs}{EXECUTOR_HOME_ROOT}/codex-unreadable")
    (home / "auth.json").mkdir(parents=True)

    with pytest.raises(HarnessFallbackError) as refused:
        asyncio.run(_turn())

    assert refused.value.kind is HarnessFallbackKind.ACCESS
    assert f"{EXECUTOR_HOME_ROOT}/codex-unreadable could not be read" in str(refused.value)
    assert _app_server_execs(fake) == []
    assert fake.homes() == [home]


def test_a_newer_stale_login_that_cannot_be_persisted_keeps_its_home_and_refuses(fake: FakeExecutor) -> None:
    home = _stale_home(fake, "newer", "2026-10-11T00:00:00Z")
    Path(f"{fake.pass_store}.readonly").touch()

    with pytest.raises(HarnessFallbackError) as refused:
        asyncio.run(_turn())

    assert refused.value.kind is HarnessFallbackKind.AUTH
    assert _app_server_execs(fake) == []
    assert fake.homes() == [home]
    assert _refresh_token(fake) == "stored"


def test_a_thread_on_a_replaced_executor_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    served = attested(fake, monkeypatch)
    manager = codex_read_only_server.executor_codex_app_server(served)
    asyncio.run(_turn())
    fake.containers = ["container-02"]
    fake.install()
    options = CodexAppServerOptions.from_sdk_options(_PLANNING, executor=served)

    try:
        with pytest.raises(HarnessFallbackError, match="the executor was replaced by container-02"):
            asyncio.run(manager.open_session(options, None))
    finally:
        fake.containers = [EXECUTOR_ID]
        fake.install()

    assert len(fake.thread_starts) == 1


def test_planted_rules_retire_the_executor_server(fake: FakeExecutor) -> None:
    def plant() -> str:
        (launch,) = _app_server_execs(fake)
        rules = Path(f"{fake.fs}{_codex_home(launch)}/rules")
        rules.mkdir()
        (rules / "planted.rules").write_text('prefix_rule(pattern = ["git"], decision = "allow")\n', encoding="utf-8")
        return _codex_home(launch)

    async def two_sessions() -> str:
        await _turn()
        planted = await asyncio.to_thread(plant)
        assert await _turn() == "OK"
        return planted

    planted = asyncio.run(two_sessions())

    homes = [_codex_home(call) for call in _app_server_execs(fake)]
    assert homes[0] == planted
    assert planted not in homes[1:]
    assert [request["home"] for request in fake.probe_requests if request["home"]] == [planted, planted, homes[1]]
    assert not Path(f"{fake.fs}{planted}").exists()


def test_each_thread_reprobes_its_roots_and_live_home(fake: FakeExecutor) -> None:
    async def two_sessions() -> None:
        await _turn()
        await _turn()

    asyncio.run(two_sessions())

    (launch,) = _app_server_execs(fake)
    per_thread = [request for request in fake.probe_requests if request["home"]]
    assert [(request["roots"], request["home"]) for request in per_thread] == [
        ([EXAMPLE_ROOT], _codex_home(launch)),
        ([EXAMPLE_ROOT], _codex_home(launch)),
    ]


def test_executor_never_takes_the_worker_lease_or_login(fake: FakeExecutor, tmp_path: Path) -> None:
    fake.rotated_login = ROTATED_LOGIN
    fake.install()
    worker_home = tmp_path / "worker-codex-home"
    worker_home.mkdir()
    fd = os.open(worker_home / ".auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert asyncio.run(asyncio.wait_for(_turn(), timeout=30)) == "OK"
    finally:
        os.close(fd)

    assert len(fake.pass_entries) == 2
    assert EXECUTOR_LOGIN_ENTRY in fake.pass_entries
    assert any(entry.startswith(f"{EXECUTOR_LOGIN_ENTRY}.bak-") for entry in fake.pass_entries)
    assert fake.pass_writes[-1] == EXECUTOR_LOGIN_ENTRY
    assert fake.stored(CODEX_AUTH_PASS_ENTRY) == WORKER_LOGIN
    assert all("worker-codex-home" not in json.dumps(call) for call in fake.calls)


def test_executor_exec_is_unprivileged_and_tokenless(fake: FakeExecutor) -> None:
    dispatch = ClaudeAgentOptions(
        cwd=EXAMPLE_ROOT,
        permission_mode="bypassPermissions",
        disallowed_tools=["Write", "Edit", "NotebookEdit"],
        env={"GH_TOKEN": "example-forge-token", ROUTED_GH_KEY_ENV: "example/forge/key"},
    )

    asyncio.run(_turn(dispatch))

    execs = [call for call in fake.calls if call[:1] == ["exec"]]
    assert execs
    assert not any(flag in call for call in execs for flag in ("--privileged", "-u", "--user"))
    assert "example-forge-token" not in json.dumps(fake.calls)
    assert all(set(env) <= {"PATH", "CODEX_HOME", "HOME"} for env in fake.docker_cli_env)


def test_executor_manager_refuses_an_attestation_for_another_container(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = attested(fake, monkeypatch)
    fake.containers = ["container-02"]
    other = attested(fake, monkeypatch)
    manager = ExecutorCodexAppServer(served)
    options = CodexAppServerOptions.from_sdk_options(_PLANNING, executor=other)

    with pytest.raises(CodexAppServerError, match=f"options not attested for executor {EXECUTOR_ID}"):
        asyncio.run(manager.open_session(options, None))

    assert manager.stopped
    assert _app_server_execs(fake) == []


_SWAPPED = {"tokens": {"account_id": "acct-other", "refresh_token": "other"}, "last_refresh": "2099-01-01T00:00:00Z"}


@pytest.mark.parametrize(
    "rotated",
    [
        _SWAPPED,
        {"last_refresh": "2099-01-01T00:00:00Z"},
        {"tokens": {"account_id": EXECUTOR_ACCOUNT}, "last_refresh": "2099-01-01T00:00:00Z"},
        {"tokens": {"account_id": "", "refresh_token": "other"}, "last_refresh": "2099-01-01T00:00:00Z"},
        '{"tokens": {"account_id": "acct-exa',
    ],
    ids=["another-account", "no-tokens", "no-refresh-token", "empty-account", "truncated"],
)
def test_a_rotated_login_that_is_not_the_executors_is_never_persisted(fake: FakeExecutor, rotated: object) -> None:
    fake.rotated_login = rotated
    fake.install()

    with pytest.raises(HarnessFallbackError) as refused:
        asyncio.run(_turn())
    codex_read_only_server.reset_executor_app_servers()

    assert fake.pass_writes == []
    assert _refresh_token(fake) == "stored"
    assert refused.value.kind is HarnessFallbackKind.AUTH
    assert EXECUTOR_LOGIN_ENTRY in str(refused.value)
    assert "--executor" in str(refused.value)
    assert CODEX_AUTH_PASS_ENTRY not in str(refused.value)


@pytest.mark.parametrize(("account", "refresh_token"), [("acct-other", None), (EXECUTOR_ACCOUNT, "")])
def test_a_newer_stale_login_that_is_not_a_complete_executor_login_is_deleted_without_recovery(
    fake: FakeExecutor, account: str, refresh_token: str | None
) -> None:
    swapped = _stale_home(fake, "swapped", "2099-01-01T00:00:00Z", account=account, refresh_token=refresh_token)

    assert asyncio.run(_turn()) == "OK"

    assert fake.pass_writes == []
    assert _refresh_token(fake) == "stored"
    assert not swapped.exists()
