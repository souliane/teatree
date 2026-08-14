"""An unreadable job trace is a VISIBLE error, never an absent one.

``fetch_pipeline_errors`` used to append a job only ``if trace:``, so a failed job
whose trace endpoint returned 403 (expired artifact / a token without
``read_build``) or errored in transport vanished from the list. The debug lane
then read ``[]`` as "this pipeline has no errors to act on" rather than "I could
not read why it failed" — the absent-signal-as-definite-verdict shape.
"""

from unittest.mock import MagicMock, patch

import httpx

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.ci import GitLabCIService


def _service(*, failed_job: str = "build") -> tuple[GitLabCIService, MagicMock]:
    client = MagicMock(spec=GitLabAPI)
    client.token = "test-token"
    client.base_url = "https://gitlab.com/api/v4"
    client.resolve_project.return_value = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
    client.get_json.return_value = [{"id": 500}]
    client.get_json_paginated.return_value = [{"id": 900, "name": failed_job, "status": "failed"}]
    return GitLabCIService(client=client), client


def _response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status_code=status, text=text, request=httpx.Request("GET", "https://gitlab.example/trace"))


class TestUnreadableTraceStaysVisible:
    def test_a_forbidden_trace_still_reports_the_failed_job(self) -> None:
        service, _ = _service()
        with patch("httpx.get", return_value=_response(403)):
            errors = service.fetch_pipeline_errors(project="org/repo", ref="main")
        assert len(errors) == 1
        assert "build" in errors[0]
        assert "trace unreadable" in errors[0]
        assert "HTTP 403" in errors[0]

    def test_a_transport_error_still_reports_the_failed_job(self) -> None:
        service, _ = _service()
        with patch("httpx.get", side_effect=httpx.ConnectTimeout("timed out")):
            errors = service.fetch_pipeline_errors(project="org/repo", ref="main")
        assert len(errors) == 1
        assert "trace unreadable" in errors[0]
        assert "ConnectTimeout" in errors[0]

    def test_a_missing_token_still_reports_the_failed_job(self) -> None:
        service, client = _service()
        client.token = ""
        errors = service.fetch_pipeline_errors(project="org/repo", ref="main")
        assert len(errors) == 1
        assert "trace unreadable" in errors[0]

    def test_a_readable_trace_reports_its_error_tail(self) -> None:
        service, _ = _service()
        trace = "setting up\nE   AssertionError: expected 3 got 4\nteardown"
        with patch("httpx.get", return_value=_response(200, trace)):
            errors = service.fetch_pipeline_errors(project="org/repo", ref="main")
        assert len(errors) == 1
        assert "AssertionError: expected 3 got 4" in errors[0]
        assert "trace unreadable" not in errors[0]

    def test_a_passing_pipeline_still_reports_nothing(self) -> None:
        # The anti-vacuity control: "no failures" must stay distinguishable from
        # "failures whose traces could not be read".
        service, client = _service()
        client.get_json_paginated.return_value = [{"id": 900, "name": "build", "status": "success"}]
        with patch("httpx.get", return_value=_response(403)):
            assert service.fetch_pipeline_errors(project="org/repo", ref="main") == []
