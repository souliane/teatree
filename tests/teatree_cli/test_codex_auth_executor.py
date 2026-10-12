"""The executor's own Codex login is imported and checked from teatree-worker only."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.agents import codex_read_only_server
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY
from teatree.agents.codex_read_only_executor import EXECUTOR_LOGIN_ENTRY
from teatree.cli.codex import codex_app
from teatree.utils import run, secrets
from tests.teatree_agents._executor_fakes import EXECUTOR_ID, WORKER_LOGIN, FakeExecutor

_FORGET_HOLDS = "teatree.cli.codex_auth._forget_codex_holds"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeExecutor]:
    executor = FakeExecutor(tmp_path)
    for key, value in executor.install().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(secrets, "run_bounded_group", run.run_bounded_group)
    monkeypatch.setattr(_FORGET_HOLDS, lambda: None)
    with executor.running():
        yield executor


@pytest.mark.parametrize(
    ("verb", "argv"),
    [("import", ["auth", "import", "--executor", "--from", "-"]), ("check", ["auth", "check", "--executor"])],
    ids=["import", "check"],
)
def test_executor_commands_outside_the_worker_name_the_worker_route(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str, argv: list[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-docker-here"))

    result = CliRunner().invoke(codex_app, argv, input="{}")

    assert result.exit_code == 1
    assert "the docker CLI is not installed here" in result.stdout
    assert f"deploy/t3 codex auth {verb} --executor" in result.stdout
    assert "teatree-worker" in result.stdout
    assert fake.pass_writes == []


def test_executor_import_stores_the_executor_entry_under_its_own_lease(fake: FakeExecutor, tmp_path: Path) -> None:
    login = {
        "tokens": {"account_id": "acct-example", "refresh_token": "imported"},
        "last_refresh": "2026-10-12T08:00:00Z",
    }

    result = CliRunner().invoke(codex_app, ["auth", "import", "--executor", "--from", "-"], input=json.dumps(login))

    assert result.exit_code == 0, result.stdout
    assert EXECUTOR_LOGIN_ENTRY in result.stdout
    assert fake.stored(EXECUTOR_LOGIN_ENTRY) == login
    assert CODEX_AUTH_PASS_ENTRY not in fake.pass_writes
    assert [entry.name for entry in (tmp_path / "data" / "codex-ro-executor").iterdir()] == [".auth.lock"]


def test_executor_check_runs_one_turn_through_the_executor(fake: FakeExecutor) -> None:
    try:
        result = CliRunner().invoke(codex_app, ["auth", "check", "--executor"])
    finally:
        codex_read_only_server.reset_executor_app_servers()

    assert result.exit_code == 0, result.stdout
    assert "Codex canary: PASS" in result.stdout
    launches = [call for call in fake.calls if call[:2] == ["exec", "-i"] and "setsid" in call]
    assert [call[call.index("setsid") - 1] for call in launches] == [EXECUTOR_ID]
    assert fake.homes() == []


def _refused_import(argv: list[str], login: object) -> str:
    result = CliRunner().invoke(codex_app, argv, input=json.dumps(login))
    assert result.exit_code != 0
    return result.output


def test_executor_import_needs_an_explicit_source(fake: FakeExecutor) -> None:
    output = _refused_import(["auth", "import", "--executor"], WORKER_LOGIN)

    assert "--executor stores a second Codex login" in output
    assert fake.pass_writes == []


@pytest.mark.parametrize(
    "login",
    [
        {**WORKER_LOGIN, "last_refresh": "2026-10-12T08:00:00Z"},
        {"tokens": {"account_id": "acct-example"}},
        {"tokens": {"refresh_token": "imported"}},
    ],
    ids=["the-workers-own-chain", "no-refresh-token", "no-account"],
)
def test_executor_import_refuses_the_workers_chain_or_an_incomplete_login(fake: FakeExecutor, login: object) -> None:
    output = _refused_import(["auth", "import", "--executor", "--from", "-"], login)

    assert "Invalid value for --from" in output
    assert fake.pass_writes == []
