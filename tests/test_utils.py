import signal
import socket
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.backends import gitlab_api
from teatree.utils import db, git, ports


def test_find_free_ports_returns_dict_of_four_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """find_free_ports returns a dict with backend, frontend, postgres, redis keys."""
    monkeypatch.setattr(ports, "port_in_use", lambda port: False)

    result = ports.find_free_ports(str(tmp_path))
    assert isinstance(result, dict)
    assert set(result.keys()) == {"backend", "frontend", "postgres", "redis"}
    assert result["backend"] >= 8001
    assert result["frontend"] >= 4201
    assert result["postgres"] >= 5432
    assert result["redis"] >= 6379


def test_find_free_ports_skips_occupied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """find_free_ports skips ports that are already in use."""
    occupied = {8001, 4201}
    monkeypatch.setattr(ports, "port_in_use", lambda port: port in occupied)

    result = ports.find_free_ports(str(tmp_path))
    assert result["backend"] > 8001  # skipped 8001
    assert result["frontend"] > 4201  # skipped 4201


def test_port_in_use_detects_bound_socket() -> None:
    """port_in_use returns True for a bound port and False for an unbound one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        sock.listen()
        occupied_port = sock.getsockname()[1]
        assert ports.port_in_use(occupied_port) is True


def test_port_in_use_returns_false_for_dummy_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """port_in_use returns False when bind succeeds."""

    class DummySocket:
        def bind(self, _address: tuple[str, int]) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(ports.socket, "socket", lambda family, sock_type: DummySocket())
    assert ports.port_in_use(12345) is False


def test_get_service_port_parses_docker_compose_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_service_port parses `docker compose port` output."""
    monkeypatch.setattr(
        ports.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 0, stdout="0.0.0.0:8042\n"),
    )
    assert ports.get_service_port("myproject", "web", 8000) == 8042


def test_get_service_port_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_service_port returns None when service is not running."""
    monkeypatch.setattr(
        ports.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 1, stdout=""),
    )
    assert ports.get_service_port("myproject", "web", 8000) is None


def test_get_worktree_ports_queries_all_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_worktree_ports queries all compose services and returns named ports."""
    port_map = {
        ("web", 8000): "0.0.0.0:8042\n",
        ("frontend", 4200): "0.0.0.0:4242\n",
        ("db", 5432): "",  # not running
        ("rd", 6379): "0.0.0.0:6380\n",
    }

    def fake_run(cmd: list[str], **kwargs: object) -> CompletedProcess[str]:
        service = cmd[-2]
        container_port = int(cmd[-1])
        key = (service, container_port)
        output = port_map.get(key, "")
        return CompletedProcess(cmd, 0 if output else 1, stdout=output)

    monkeypatch.setattr(ports.subprocess, "run", fake_run)

    result = ports.get_worktree_ports("myproject")
    assert result == {"backend": 8042, "frontend": 4242, "redis": 6380}
    assert "postgres" not in result  # db service was not running


def test_default_branch_prefers_symbolic_ref_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        calls.append(args)
        if args[-1] == "refs/remotes/origin/HEAD":
            return CompletedProcess(args, 1, "", "")
        if args[-1] == "refs/remotes/origin/main":
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    assert git.default_branch("/tmp/repo") == "main"
    assert calls[0][-1] == "refs/remotes/origin/HEAD"


def test_default_branch_returns_symbolic_ref_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        git.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(list(args[0]), 0, "refs/remotes/origin/main\n", ""),
    )

    assert git.default_branch("/tmp/repo") == "main"


def test_git_helpers_cover_run_check_current_branch_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        if args[-2:] == ["status", "--short"]:
            return CompletedProcess(args, 0, " M pyproject.toml\n", "")
        if args[-2:] == ["rev-parse", "--abbrev-ref"]:
            return CompletedProcess(args, 0, "", "")
        if args[-1] == "refs/remotes/origin/HEAD":
            raise git.subprocess.CalledProcessError(1, args)
        return CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    assert git.run(repo="/tmp/repo", args=["status", "--short"]) == "M pyproject.toml"
    assert git.check(repo="/tmp/repo", args=["status", "--short"]) is True
    assert git.current_branch("/tmp/repo") == ""
    with pytest.raises(RuntimeError, match="Could not detect default branch"):
        git.default_branch("/tmp/repo")


def test_run_checked_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str], *, capture_output: bool, text: bool = False, check: bool = False
    ) -> CompletedProcess[str]:
        if check:
            raise git.subprocess.CalledProcessError(1, args)
        return CompletedProcess(args, 0, "ok\n", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    assert git.run(repo="/tmp/r", args=["log"]) == "ok"
    with pytest.raises(git.subprocess.CalledProcessError):
        git.run_checked(repo="/tmp/r", args=["bad"])


def test_git_high_level_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, capture_output: bool, text: bool = False, check: bool = False
    ) -> CompletedProcess[str]:
        calls.append(list(args))
        if "merge-base" in args:
            return CompletedProcess(args, 0, "abc123\n", "")
        if "rev-list" in args:
            return CompletedProcess(args, 0, "3\n", "")
        if "log" in args:
            return CompletedProcess(args, 0, "abc feat one\ndef feat two\n", "")
        if "status" in args:
            return CompletedProcess(args, 0, " M file.py\n", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    assert git.merge_base("/tmp/r", "origin/main") == "abc123"
    assert git.rev_count("/tmp/r", "abc123..HEAD") == 3
    assert git.log_oneline("/tmp/r", "abc123..HEAD") == "abc feat one\ndef feat two"
    assert git.status_porcelain("/tmp/r") == "M file.py"

    git.soft_reset("/tmp/r", "abc123")
    assert any("reset" in c for c in calls)

    git.commit("/tmp/r", "squash msg")
    assert any("commit" in c for c in calls)

    git.fetch("/tmp/r", "origin", "main")
    assert any("fetch" in c for c in calls)

    git.rebase("/tmp/r", "origin/main")
    assert any("rebase" in c for c in calls)


def test_git_worktree_and_branch_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str], *, capture_output: bool, text: bool = False, check: bool = False
    ) -> CompletedProcess[str]:
        if "worktree" in args:
            return CompletedProcess(args, 0, "", "")
        if "branch" in args:
            return CompletedProcess(args, 1, "", "")
        if "pull" in args:
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    assert git.worktree_remove("/tmp/r", "/tmp/wt") is True
    assert git.branch_delete("/tmp/r", "old-branch") is False
    assert git.pull_ff_only("/tmp/r") is True


def test_fetch_without_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, capture_output: bool, text: bool = False, check: bool = False
    ) -> CompletedProcess[str]:
        calls.append(list(args))
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    git.fetch("/tmp/r")
    assert calls[-1] == ["git", "-C", "/tmp/r", "fetch", "origin"]


def test_free_port_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "port_in_use", lambda _port: True)
    monkeypatch.setattr(
        ports.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 0, stdout="12345\n"),
    )
    killed: list[tuple[int, int]] = []
    with patch("os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
        assert ports.free_port(8001) == 12345
    assert killed == [(12345, signal.SIGTERM)]


def test_free_port_returns_none_when_not_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "port_in_use", lambda _port: False)
    assert ports.free_port(8001) is None


def test_free_port_returns_none_when_lsof_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "port_in_use", lambda _port: True)
    monkeypatch.setattr(
        ports.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 1, stdout=""),
    )
    assert ports.free_port(8001) is None


def test_db_restore_uses_pg_restore_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "toc", "")
        if args[0] == "pg_restore":
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    db.db_restore("wt_123", "/tmp/dump.dump")

    assert commands[0][:2] == ["dropdb", "-h"]
    assert commands[1][:2] == ["createdb", "-h"]
    assert commands[2] == ["pg_restore", "-l", "/tmp/dump.dump"]
    assert commands[3][0] == "pg_restore"


def test_db_helpers_cover_env_exists_and_psql_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_USER", "worker")

    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql" and "-lqt" in args:
            return CompletedProcess(args, 0, "wt_42 | owner\n", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    assert db.pg_env().get("PGPASSWORD") is None
    assert db.pg_host() == "db.internal"
    assert db.pg_user() == "worker"
    assert db.worktree_db_name("42", "") == "wt_42"
    db.db_restore("wt_42", "/tmp/dump.sql")
    assert db.db_exists("wt_42") is True
    assert commands[3][0] == "psql"


def test_db_restore_raises_when_restore_commands_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "toc", "")
        if args[0] == "pg_restore":
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 1, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pg_restore failed"):
        db.db_restore("wt_55", "/tmp/dump.dump")


def test_db_restore_raises_when_psql_restore_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 1, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="psql restore failed"):
        db.db_restore("wt_56", "/tmp/dump.sql")


def test_db_restore_detects_truncated_pg_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "TOC", "")
        if args[0] == "pg_restore" and "-d" in args:
            return CompletedProcess(args, 0, "", "WARNING: could not read data")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Corrupt or truncated dump"):
        db.db_restore("wt_70", "/tmp/dump.pgdump")


def test_db_restore_detects_truncated_psql(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 0, "", "unexpected EOF on client connection")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Corrupt or truncated dump"):
        db.db_restore("wt_71", "/tmp/dump.sql")


def test_gitlab_api_resolves_remote_project(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool = False,
    ) -> CompletedProcess[str]:
        return CompletedProcess(args, 0, "git@gitlab.com:acme/platform.git\n", "")

    monkeypatch.setattr(gitlab_api.subprocess, "run", fake_run)

    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "id": 42,
            "path_with_namespace": "acme/platform",
            "path": "platform",
        },
    )

    project = client.resolve_project_from_remote("/tmp/repo")

    assert project is not None
    assert project.project_id == 42
    assert project.short_name == "platform"


def test_gitlab_api_helpers_cover_http_paths_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        requests.append(url)

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object] | list[dict[str, object]]:
                if url.endswith("projects/acme%2Fplatform"):
                    return {"id": 42, "path_with_namespace": "acme/platform", "path": "platform"}
                if "merge_requests" in url:
                    return [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
                return [{"id": 101}]

        return Response()

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        requests.append(url)

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"ok": True}

        return Response()

    monkeypatch.setattr(gitlab_api.httpx, "get", fake_get)
    monkeypatch.setattr(gitlab_api.httpx, "post", fake_post)
    monkeypatch.setattr(
        gitlab_api.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(list(args[0]), 1, "", ""),
    )

    client = gitlab_api.GitLabAPI(token="")
    assert client.get_json("projects/x") is None
    assert client.post_json("projects/x") is None

    client = gitlab_api.GitLabAPI(token="test-token")
    assert client.resolve_project("acme/platform") == client.resolve_project("acme/platform")
    assert client.resolve_project("missing/project") is None
    assert client.list_all_open_mrs("adrien") == [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
    assert client.list_all_open_mrs("adrien", include_draft=False) == [{"iid": 1, "draft": False}]
    assert client.cancel_pipelines(42, "feature") == [101, 101]
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)
    assert client.list_all_open_mrs("adrien") == []
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"oops": "bad"})
    assert client.cancel_pipelines(42, "feature") == []
    assert client.resolve_project_from_remote("/tmp/repo") is None
    assert gitlab_api.GitLabAPI.current_branch("/tmp/repo") == ""
    assert requests


def test_gitlab_api_returns_none_for_non_gitlab_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gitlab_api.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(list(args[0]), 0, "https://example.com/acme/repo.git\n", ""),
    )

    assert gitlab_api.GitLabAPI(token="test-token").resolve_project_from_remote("/tmp/repo") is None


def test_pg_env_includes_port_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    env = db.pg_env()

    assert env["PGPORT"] == "5433"
    assert env["PGPASSWORD"] == "secret"


def test_pg_env_omits_port_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    env = db.pg_env()

    assert "PGPORT" not in env


def test_gitlab_api_graphql_sends_post_request(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, object]] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        posted.append({"url": url, "json": json})

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"project": {"workItems": {"nodes": []}}}}

        return Response()

    monkeypatch.setattr(gitlab_api.httpx, "post", fake_post)

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.graphql("query { project { id } }", {"projectPath": "org/repo"})

    assert result is not None
    assert result["data"] is not None
    assert posted[0]["url"] == "https://gitlab.com/api/graphql"


def test_gitlab_api_graphql_returns_none_without_token() -> None:
    client = gitlab_api.GitLabAPI(token="")

    result = client.graphql("query { project { id } }")

    assert result is None


def test_get_work_item_status_returns_status_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "DESCRIPTION"},
                                    {"type": "STATUS", "status": {"name": "In Progress"}},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result == "In Progress"


def test_get_work_item_status_returns_none_when_graphql_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "graphql", lambda query, variables: None)

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_for_empty_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_when_no_status_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "DESCRIPTION"},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_when_widgets_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": "not-a-list",
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_when_status_value_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "STATUS", "status": "not-a-dict"},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_list_all_open_mrs_with_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"iid": 1, "draft": False}],
    )

    result = client.list_all_open_mrs("adrien", updated_after="2024-01-01T00:00:00Z")

    assert result == [{"iid": 1, "draft": False}]


def test_list_recently_merged_mrs_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"iid": 10, "state": "merged"}],
    )

    result = client.list_recently_merged_mrs("adrien")

    assert result == [{"iid": 10, "state": "merged"}]


def test_list_recently_merged_mrs_with_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured_endpoints: list[str] = []

    def capture_get_json(endpoint: str) -> list[dict[str, object]]:
        captured_endpoints.append(endpoint)
        return [{"iid": 10}]

    monkeypatch.setattr(client, "get_json", capture_get_json)

    result = client.list_recently_merged_mrs("adrien", updated_after="2024-06-01T00:00:00Z")

    assert result == [{"iid": 10}]
    assert "updated_after" in captured_endpoints[0]


def test_list_recently_merged_mrs_returns_empty_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"error": "bad"})

    result = client.list_recently_merged_mrs("adrien")

    assert result == []


def test_get_mr_pipeline_returns_status_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"status": "success", "web_url": "https://gitlab.com/pipelines/1"}],
    )

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": "success", "url": "https://gitlab.com/pipelines/1"}


def test_get_mr_pipeline_returns_none_when_no_pipelines(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: [])

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": None, "url": None}


def test_get_mr_pipeline_returns_none_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": None, "url": None}


def test_get_mr_approvals_returns_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "approved_by": [{"user": {"username": "reviewer1"}}, {"user": {"username": "reviewer2"}}],
            "approvals_required": 2,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 2, "required": 2, "approved_by": ["reviewer1", "reviewer2"]}


def test_get_mr_approvals_returns_defaults_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": []}


def test_get_mr_approvals_handles_non_list_approved_by(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"approved_by": "not-a-list", "approvals_required": 1},
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": []}


def test_get_mr_approvals_skips_non_dict_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_mr_approvals skips non-dict entries in approved_by list (line 213)."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "approved_by": [
                "not-a-dict",
                {"user": {"username": "reviewer1"}},
            ],
            "approvals_required": 1,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 2, "required": 1, "approved_by": ["reviewer1"]}


def test_get_issue_returns_issue_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"iid": 5, "title": "Bug"},
    )

    result = client.get_issue(42, 5)

    assert result == {"iid": 5, "title": "Bug"}


def test_get_issue_returns_none_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_issue(42, 5)

    assert result is None


def test_get_mr_discussions_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"id": "d1", "notes": []}],
    )

    result = client.get_mr_discussions(42, 1)

    assert result == [{"id": "d1", "notes": []}]


def test_get_mr_discussions_returns_empty_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_discussions(42, 1)

    assert result == []


def test_current_username_returns_username(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"username": "adrien"},
    )

    result = client.current_username()

    assert result == "adrien"


def test_current_username_returns_empty_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.current_username()

    assert result == ""


def test_gitlab_api_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")

    client = gitlab_api.GitLabAPI()

    assert client.token == "env-token"


def test_gitlab_api_explicit_token_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")

    client = gitlab_api.GitLabAPI(token="explicit-token")

    assert client.token == "explicit-token"


def test_resolve_token_falls_back_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teatree.utils.secrets.read_pass", return_value="pass-token"):
        assert gitlab_api._resolve_token() == "pass-token"


def test_resolve_token_returns_empty_when_pass_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teatree.utils.secrets.read_pass", return_value=""):
        assert gitlab_api._resolve_token() == ""


def test_resolve_token_prefers_env_over_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")
    assert gitlab_api._resolve_token() == "env-token"


class TestGitLabAPICacheHits:
    """Verify cache-hit branches for all cached methods."""

    def test_get_work_item_status_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        graphql_calls: list[int] = []
        monkeypatch.setattr(
            client,
            "graphql",
            lambda q, v: (
                graphql_calls.append(1)
                or {
                    "data": {
                        "project": {
                            "workItems": {
                                "nodes": [{"widgets": [{"type": "STATUS", "status": {"name": "In progress"}}]}]
                            }
                        }
                    }
                }
            ),
        )
        assert client.get_work_item_status("org/repo", 1) == "In progress"
        assert client.get_work_item_status("org/repo", 1) == "In progress"
        assert len(graphql_calls) == 1

    def test_get_mr_pipeline_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(
            client,
            "get_json",
            lambda ep: calls.append(1) or [{"status": "success", "web_url": "https://ci/1"}],
        )
        client.get_mr_pipeline(1, 1)
        result = client.get_mr_pipeline(1, 1)
        assert result["status"] == "success"
        assert len(calls) == 1

    def test_get_mr_approvals_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(
            client,
            "get_json",
            lambda ep: calls.append(1) or {"approved_by": [], "approvals_required": 1},
        )
        client.get_mr_approvals(1, 1)
        result = client.get_mr_approvals(1, 1)
        assert result["count"] == 0
        assert len(calls) == 1

    def test_get_issue_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"iid": 5, "title": "Bug"})
        client.get_issue(1, 5)
        result = client.get_issue(1, 5)
        assert result is not None
        assert result["title"] == "Bug"
        assert len(calls) == 1

    def test_get_mr_discussions_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or [{"id": "d1"}])
        client.get_mr_discussions(1, 1)
        result = client.get_mr_discussions(1, 1)
        assert result == [{"id": "d1"}]
        assert len(calls) == 1

    def test_current_username_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"username": "dev"})
        client.current_username()
        result = client.current_username()
        assert result == "dev"
        assert len(calls) == 1

    def test_clear_response_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"username": "dev"})
        client.current_username()
        client.clear_response_cache()
        client.current_username()
        assert len(calls) == 2
