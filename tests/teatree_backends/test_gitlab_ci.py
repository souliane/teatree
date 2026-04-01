"""Tests for the GitLab CI backend."""

from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab_ci import GitLabCIService, _extract_error_tail


def _make_client(*, project: ProjectInfo | None = None) -> tuple[GitLabAPI, MagicMock]:
    client = MagicMock(spec=GitLabAPI)
    client.token = "test-token"
    client.base_url = "https://gitlab.com/api/v4"
    if project:
        client.resolve_project.return_value = project
    else:
        client.resolve_project.return_value = None
    return client, client


def _project() -> ProjectInfo:
    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")


def test_cancel_pipelines_delegates_to_gitlab_api() -> None:
    client, mock = _make_client(project=_project())
    mock.cancel_pipelines.return_value = [100, 101]
    service = GitLabCIService(client=client)

    result = service.cancel_pipelines(project="org/repo", ref="main")

    assert result == [100, 101]
    mock.cancel_pipelines.assert_called_once_with(42, "main")


def test_cancel_pipelines_returns_empty_for_unknown_project() -> None:
    client, _ = _make_client()
    service = GitLabCIService(client=client)

    result = service.cancel_pipelines(project="unknown/repo", ref="main")

    assert result == []


def test_fetch_failed_tests_extracts_from_test_report() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 500}],  # latest pipeline
        {  # test report
            "test_suites": [
                {
                    "test_cases": [
                        {"status": "success", "classname": "tests.test_a", "name": "test_ok"},
                        {"status": "failed", "classname": "tests.test_b", "name": "test_broken"},
                    ],
                },
            ],
        },
    ]
    service = GitLabCIService(client=client)

    result = service.fetch_failed_tests(project="org/repo", ref="main")

    assert result == ["tests.test_b::test_broken"]


def test_fetch_failed_tests_returns_empty_for_no_pipeline() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.return_value = []
    service = GitLabCIService(client=client)

    result = service.fetch_failed_tests(project="org/repo", ref="main")

    assert result == []


def test_fetch_pipeline_errors_extracts_from_failed_jobs() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 600}],  # latest pipeline
        [  # jobs
            {"id": 1, "name": "test", "status": "failed"},
            {"id": 2, "name": "lint", "status": "success"},
        ],
    ]

    with patch.object(service := GitLabCIService(client=client), "_get_job_trace", return_value="FAILED in test_foo"):
        result = service.fetch_pipeline_errors(project="org/repo", ref="main")

    assert len(result) == 1
    assert "test" in result[0]


def test_trigger_pipeline_posts_to_api() -> None:
    client, mock = _make_client(project=_project())
    mock.post_json.return_value = {"id": 700, "web_url": "https://gitlab.com/pipelines/700"}
    service = GitLabCIService(client=client)

    result = service.trigger_pipeline(project="org/repo", ref="develop", variables={"E2E": "true"})

    assert result["id"] == 700
    mock.post_json.assert_called_once()
    call_args = mock.post_json.call_args
    assert call_args[0][0] == "projects/42/pipeline"
    payload = call_args[0][1]
    assert payload["ref"] == "develop"


def test_quality_check_returns_test_counts() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 800, "status": "success"}],  # latest pipeline
        {  # test report
            "total_count": 100,
            "success_count": 95,
            "failed_count": 5,
            "error_count": 0,
        },
    ]
    service = GitLabCIService(client=client)

    result = service.quality_check(project="org/repo", ref="main")

    assert result["total_count"] == 100
    assert result["failed_count"] == 5


def test_extract_error_tail_finds_error_section() -> None:
    trace = "line 1\nline 2\nFAILED test_foo.py\nassert 1 == 2\nline 5"
    result = _extract_error_tail(trace, max_lines=10)

    assert "FAILED" in result
    assert "assert" in result


def test_extract_error_tail_falls_back_to_last_lines() -> None:
    trace = "\n".join(f"line {i}" for i in range(100))
    result = _extract_error_tail(trace, max_lines=5)

    assert result.count("\n") == 4  # 5 lines


def test_extract_error_tail_truncates_at_max_lines() -> None:
    # Build a trace where every line matches the error pattern, exceeding max_lines
    trace = "\n".join(f"ERROR at step {i}" for i in range(20))
    result = _extract_error_tail(trace, max_lines=3)

    assert result.count("\n") == 2  # exactly 3 lines


def test_fetch_pipeline_errors_returns_message_for_unknown_project() -> None:
    client, _ = _make_client()
    service = GitLabCIService(client=client)

    result = service.fetch_pipeline_errors(project="unknown/repo", ref="main")

    assert result == ["Could not resolve project: unknown/repo"]


def test_fetch_pipeline_errors_returns_message_for_no_pipeline() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.return_value = []  # empty pipeline list
    service = GitLabCIService(client=client)

    result = service.fetch_pipeline_errors(project="org/repo", ref="main")

    assert result == ["No pipeline found for ref: main"]


def test_fetch_pipeline_errors_returns_empty_when_jobs_not_a_list() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 600}],  # latest pipeline
        {"error": "not a list"},  # jobs response is a dict, not a list
    ]
    service = GitLabCIService(client=client)

    result = service.fetch_pipeline_errors(project="org/repo", ref="main")

    assert result == []


def test_fetch_pipeline_errors_skips_empty_trace() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 600}],  # latest pipeline
        [{"id": 1, "name": "test", "status": "failed"}],  # jobs
    ]

    with patch.object(service := GitLabCIService(client=client), "_get_job_trace", return_value=""):
        result = service.fetch_pipeline_errors(project="org/repo", ref="main")

    assert result == []


def test_fetch_failed_tests_returns_empty_for_unknown_project() -> None:
    client, _ = _make_client()
    service = GitLabCIService(client=client)

    result = service.fetch_failed_tests(project="unknown/repo", ref="main")

    assert result == []


def test_fetch_failed_tests_returns_empty_when_report_not_a_dict() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 500}],  # latest pipeline
        [{"not": "a dict"}],  # test report is a list, not a dict
    ]
    service = GitLabCIService(client=client)

    result = service.fetch_failed_tests(project="org/repo", ref="main")

    assert result == []


def test_fetch_failed_tests_handles_non_dict_suite_and_case() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 500}],  # latest pipeline
        {
            "test_suites": [
                "not-a-dict",  # non-dict suite gets skipped
                {
                    "test_cases": [
                        "not-a-dict",  # non-dict case gets skipped
                        {"status": "failed", "name": "test_only_name"},  # no classname
                    ],
                },
            ],
        },
    ]
    service = GitLabCIService(client=client)

    result = service.fetch_failed_tests(project="org/repo", ref="main")

    assert result == ["test_only_name"]


def test_trigger_pipeline_returns_error_for_unknown_project() -> None:
    client, _ = _make_client()
    service = GitLabCIService(client=client)

    result = service.trigger_pipeline(project="unknown/repo", ref="main")

    assert result == {"error": "Could not resolve project: unknown/repo"}


def test_trigger_pipeline_without_variables() -> None:
    client, mock = _make_client(project=_project())
    mock.post_json.return_value = {"id": 701}
    service = GitLabCIService(client=client)

    result = service.trigger_pipeline(project="org/repo", ref="main")

    assert result["id"] == 701
    payload = mock.post_json.call_args[0][1]
    assert "variables" not in payload


def test_trigger_pipeline_returns_empty_dict_when_post_returns_none() -> None:
    client, mock = _make_client(project=_project())
    mock.post_json.return_value = None
    service = GitLabCIService(client=client)

    result = service.trigger_pipeline(project="org/repo", ref="main")

    assert result == {}


def test_quality_check_returns_error_for_unknown_project() -> None:
    client, _ = _make_client()
    service = GitLabCIService(client=client)

    result = service.quality_check(project="unknown/repo", ref="main")

    assert result == {"error": "Could not resolve project: unknown/repo"}


def test_quality_check_returns_error_for_no_pipeline() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.return_value = []  # no pipelines
    service = GitLabCIService(client=client)

    result = service.quality_check(project="org/repo", ref="main")

    assert result == {"error": "No pipeline found for ref: main"}


def test_quality_check_returns_status_when_report_not_a_dict() -> None:
    client, mock = _make_client(project=_project())
    mock.get_json.side_effect = [
        [{"id": 800, "status": "running"}],  # latest pipeline
        [{"not": "a dict"}],  # test report is a list, not a dict
    ]
    service = GitLabCIService(client=client)

    result = service.quality_check(project="org/repo", ref="main")

    assert result == {"pipeline_id": 800, "status": "running"}


def test_get_job_trace_returns_empty_without_token() -> None:
    client, mock = _make_client(project=_project())
    mock.token = ""
    service = GitLabCIService(client=client)

    result = service._get_job_trace(42, 1)

    assert result == ""


def test_get_job_trace_returns_text_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx  # noqa: PLC0415

    client, _mock = _make_client(project=_project())
    service = GitLabCIService(client=client)

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.text = "Job output trace here"
        return resp

    monkeypatch.setattr(_httpx, "get", fake_get)

    result = service._get_job_trace(42, 1)

    assert result == "Job output trace here"


def test_get_job_trace_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx  # noqa: PLC0415

    client, _mock = _make_client(project=_project())
    service = GitLabCIService(client=client)

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> MagicMock:
        resp = MagicMock()
        resp.is_success = False
        return resp

    monkeypatch.setattr(_httpx, "get", fake_get)

    result = service._get_job_trace(42, 1)

    assert result == ""


def test_default_client_is_created_when_none_provided() -> None:
    service = GitLabCIService()

    assert isinstance(service._client, GitLabAPI)
