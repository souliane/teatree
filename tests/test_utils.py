import socket
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import db, git, gitlab_api, ports


def test_find_free_ports_scans_existing_env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_dir = tmp_path / "workspace"
    first_env = workspace_dir / "ticket-1" / "backend" / ".env.worktree"
    first_env.parent.mkdir(parents=True)
    first_env.write_text(
        "\n".join(
            [
                "BACKEND_PORT=8001",
                "FRONTEND_PORT=4201",
                "POSTGRES_PORT=5433",
            ],
        ),
        encoding="utf-8",
    )

    second_env = workspace_dir / "ticket-2" / "frontend" / ".env.worktree"
    second_env.parent.mkdir(parents=True)
    second_env.write_text(
        "\n".join(
            [
                "DJANGO_RUNSERVER_PORT=8002",
                "FRONTEND_PORT=4202",
            ],
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ports, "port_in_use", lambda port: False)

    assert ports.find_free_ports(str(workspace_dir), share_db_server=False) == (8003, 4203, 5434, 6379)
    assert ports.find_free_ports(str(workspace_dir), share_db_server=True) == (8003, 4203, 5432, 6379)


def test_ports_helpers_cover_socket_and_exclusions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        sock.listen()
        occupied_port = sock.getsockname()[1]
        assert ports.port_in_use(occupied_port) is True

    workspace_dir = tmp_path / "workspace"
    ignored_env = workspace_dir / "ticket-3" / "repo" / ".env.worktree"
    ignored_env.parent.mkdir(parents=True)
    ignored_env.write_text("BACKEND_PORT=8003\nPOSTGRES_PORT=invalid", encoding="utf-8")

    deep_env = workspace_dir / "too" / "deep" / "repo" / ".env.worktree"
    deep_env.parent.mkdir(parents=True)
    deep_env.write_text("BACKEND_PORT=9999", encoding="utf-8")

    checked_ports: list[int] = []

    def fake_port_in_use(port: int) -> bool:
        checked_ports.append(port)
        return port == 8001

    monkeypatch.setattr(ports, "port_in_use", fake_port_in_use)

    assert ports.find_free_ports(str(workspace_dir), exclude_dir=str(ignored_env.parent), share_db_server=False) == (
        8002,
        4201,
        5433,
        6379,
    )
    assert 8001 in checked_ports


def test_ports_low_level_helpers_cover_free_port_and_invalid_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummySocket:
        def bind(self, _address: tuple[str, int]) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(ports.socket, "socket", lambda family, sock_type: DummySocket())
    assert ports.port_in_use(12345) is False
    assert ports._parse_port_line("POSTGRES_PORT=bad", "POSTGRES_PORT=") is None

    env_file = tmp_path / ".env.worktree"
    env_file.write_text("POSTGRES_PORT=5433\nIGNORED=true\nBACKEND_PORT=8001\n", encoding="utf-8")
    used_backend: set[int] = set()
    used_frontend: set[int] = set()
    used_postgres: set[int] = set()

    ports._collect_used_ports(env_file, used_backend, used_frontend, used_postgres)

    assert used_postgres == {5433}


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
