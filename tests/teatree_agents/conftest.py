import asyncio
from pathlib import Path
from typing import Any

import pytest

from teatree.agents import codex_app_server_options
from teatree.skill_support import index as skill_index
from teatree.utils import secrets


@pytest.fixture(autouse=True)
def codex_on_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI jobs run in containers; pin the venue so host expectations never flip on a runner.
    monkeypatch.setattr(codex_app_server_options, "running_in_container", lambda: False)


@pytest.fixture(autouse=True)
def no_real_pass_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pass` fails as if absent: the Codex auth cache binds its readers as default args, so patching them misses."""
    real = secrets.run_bounded_group

    def guarded(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv and argv[0] == "pass":
            raise FileNotFoundError(argv[0])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(secrets, "run_bounded_group", guarded)


@pytest.fixture(autouse=True)
def no_real_codex_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that reaches `codex_command()` cannot start the developer's real `codex`."""
    real = asyncio.create_subprocess_exec

    async def guarded(program: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(str(program)).name == "codex":
            raise FileNotFoundError(program)
        return await real(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded)


@pytest.fixture(autouse=True)
def no_real_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("T3_CODEX_HOME", str(tmp_path / "codex-home"))


@pytest.fixture
def a_codex_login(tmp_path: Path) -> Path:
    """A stand-in login in the private Codex home, for tests that need the Codex candidate available."""
    home = tmp_path / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{}")
    return home


@pytest.fixture(autouse=True)
def installed_agent_test_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real fixture bodies satisfy strict preflight without the developer's installed stack skills; registered as an
    # install root because a folder no installer manages may not satisfy a declared apm pin.
    fixture_skills = Path(__file__).parents[1] / "fixtures" / "agent_skills"
    original_roots = skill_index.install_roots
    monkeypatch.setattr(skill_index, "install_roots", lambda: [fixture_skills, *original_roots()])
