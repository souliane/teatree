from subprocess import CompletedProcess

import pytest

from teatree.utils import ports
from teatree.utils import run as utils_run_mod


def test_get_service_port_parses_docker_compose_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_service_port parses `docker compose port` output."""
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 0, stdout="0.0.0.0:8042\n"),
    )
    assert ports.get_service_port("myproject", "web", 8000) == 8042


def test_get_service_port_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_service_port returns None when service is not running."""
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 1, stdout=""),
    )
    assert ports.get_service_port("myproject", "web", 8000) is None


def test_get_worktree_ports_queries_all_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_worktree_ports queries all compose services and returns named ports."""
    port_map = {
        ("web", 8000): "0.0.0.0:8042\n",
        ("frontend", 80): "0.0.0.0:4242\n",
        ("db", 5432): "",  # not running
    }

    def fake_run(cmd: list[str], **kwargs: object) -> CompletedProcess[str]:
        service = cmd[-2]
        container_port = int(cmd[-1])
        key = (service, container_port)
        output = port_map.get(key, "")
        return CompletedProcess(cmd, 0 if output else 1, stdout=output)

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    result = ports.get_worktree_ports("myproject")
    assert result == {"backend": 8042, "frontend": 4242}
    assert "postgres" not in result  # db service was not running
