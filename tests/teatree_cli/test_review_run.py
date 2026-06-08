"""``t3 review run <MR_URL>`` review-shape audit (#1206).

The command is read-only: it fetches MR metadata, classifies complexity,
counts unresolved discussions + draft notes + approvals, and emits a
JSON summary to stdout. These tests pin three branches that the issue
acceptance criteria call out:

* GitLab MR URL → JSON summary on stdout with ``mr``, ``forge``,
    ``changes``, ``complexity``, ``existing_review``, ``findings_catalog``,
    and ``verdict`` keys. Exit code 0.
* GitHub PR URL → ``unsupported_forge`` JSON error, exit code 2 (so the
    skill prompts can detect the unsupported branch deterministically
    instead of relying on prose-level "only GitLab" rules).
* Malformed URL → ``bad_url`` JSON error, exit code 2.

The GitLab branch patches :class:`teatree.backends.gitlab.api.GitLabAPI`
so no network is touched. Mirrors the sibling
``test_review_shape_gate.py`` stub style.
"""

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.review import review_app

pytestmark = pytest.mark.django_db

type JSONObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class _ProjectInfo:
    """Mirrors the production ``teatree.backends.gitlab.api.ProjectInfo`` shape.

    The attribute name (``project_id``) is load-bearing: the audit code
    reads ``project.project_id``, not ``project.id``. A test stub that
    misnames the attribute masks the AttributeError the real backend
    would raise on the first call against an actual GitLab instance.
    """

    project_id: int
    full_path: str


class _StubGitLabAPI:
    """In-memory stub for the GitLab API used by ``t3 review run``."""

    def __init__(self, *, changes: JSONObject, discussions: list[JSONObject]) -> None:
        self._changes = changes
        self._discussions = discussions
        self.endpoints: list[str] = []

    # The audit constructor passes ``token=...`` and ``base_url=...``;
    # the stub ignores both and just records what is called.
    def __init_subclass__(cls, **_kwargs: Any) -> None: ...  # pragma: no cover

    def get_json(self, endpoint: str) -> object:
        self.endpoints.append(endpoint)
        if endpoint.endswith("/changes"):
            return self._changes
        return None

    def resolve_project(self, repo: str) -> _ProjectInfo:
        return _ProjectInfo(project_id=42, full_path=repo)

    def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[JSONObject]:
        del project_id, mr_iid
        return self._discussions

    def get_draft_notes_count(self, project_id: int, mr_iid: int) -> int:
        del project_id, mr_iid
        return 0

    def get_mr_approvals(self, project_id: int, mr_iid: int) -> JSONObject:
        del project_id, mr_iid
        return {"count": 0, "required": 1, "approved_by": []}


def _diff(*, added: int, removed: int) -> str:
    """Build a unified-diff snippet with ``added`` ``+`` lines and ``removed`` ``-`` lines."""
    body = ["@@ -1,1 +1,1 @@"]
    body.extend(["+new" for _ in range(added)])
    body.extend(["-old" for _ in range(removed)])
    return "\n".join(body)


class TestReviewRunHappyPath:
    """A well-formed GitLab MR URL produces a JSON audit on stdout."""

    def test_emits_structured_json_with_audit_fields(self) -> None:
        runner = CliRunner()
        stub = _StubGitLabAPI(
            changes={
                "changes": [
                    {"new_path": "src/foo.py", "diff": _diff(added=5, removed=2)},
                    {"new_path": "tests/test_foo.py", "diff": _diff(added=8, removed=0)},
                ],
            },
            discussions=[
                {"notes": [{"resolved": False, "body": "still open"}]},
                {"notes": [{"resolved": True, "body": "fixed"}]},
            ],
        )
        url = "https://gitlab.com/org/proj/-/merge_requests/42"

        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=stub),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["mr"] == "org/proj!42"
        assert payload["forge"] == "gitlab"
        assert payload["url"] == url
        assert payload["changes"] == {"files": 2, "additions": 13, "deletions": 2}
        assert payload["complexity"] == "trivial"
        assert payload["existing_review"]["open_discussions"] == 1
        assert payload["existing_review"]["draft_notes"] == 0
        assert payload["existing_review"]["approvals"] == 0
        # An open discussion is enough to flag the MR as needs_attention.
        assert payload["verdict"] == "needs_attention"


class TestReviewRunUnsupportedForge:
    """A GitHub PR URL returns ``unsupported_forge`` so the skills can branch."""

    def test_github_pr_url_exits_two_with_error_json(self) -> None:
        runner = CliRunner()

        result = runner.invoke(review_app, ["run", "https://github.com/souliane/teatree/pull/1"])

        assert result.exit_code == 2, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload == {
            "error": "unsupported_forge",
            "forge": "github",
            "url": "https://github.com/souliane/teatree/pull/1",
        }


class TestReviewRunBadUrl:
    """A non-MR/PR URL exits 2 with ``bad_url`` — never a masquerading success."""

    def test_garbage_url_exits_two(self) -> None:
        runner = CliRunner()

        result = runner.invoke(review_app, ["run", "not-a-url"])

        assert result.exit_code == 2, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["error"] == "bad_url"


class TestReviewRunHttpError:
    """A backend HTTP error (401/403/404 from GitLab) maps to ``api_unavailable`` — never raw traceback.

    ``GitLabHTTPClient.get_json()`` calls ``response.raise_for_status()``,
    so a real inaccessible-repo response raises ``httpx.HTTPStatusError``.
    The audit must normalize that into the same structured exit-1 shape
    the null-payload branch uses, so the reviewer sub-agent has exactly
    one error contract to handle.
    """

    def test_http_status_error_maps_to_api_unavailable(self) -> None:
        import httpx  # noqa: PLC0415

        runner = CliRunner()

        class _HttpErrAPI:
            def get_json(self, endpoint: str) -> object:
                del endpoint
                request = httpx.Request("GET", "https://gitlab.example/api/v4/whatever")
                response = httpx.Response(status_code=403, request=request)
                msg = "forbidden"
                raise httpx.HTTPStatusError(msg, request=request, response=response)

            def resolve_project(self, repo: str) -> object:
                del repo
                return None

        url = "https://gitlab.com/org/proj/-/merge_requests/55"
        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=_HttpErrAPI()),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["error"] == "api_unavailable"
        assert payload["url"] == url
        assert "verdict" not in payload


class TestReviewRunApiUnavailable:
    """Missing token / inaccessible repo surfaces as ``api_unavailable`` — never a fake ``ready_to_review``.

    Regression guard: an earlier draft of the audit fell through to
    ``files=0, additions=0, deletions=0`` and emitted ``verdict:
    ready_to_review`` when the GitLab API returned no data (no token,
    repo not found). That is exactly the "masquerading success" the
    command's docstring forbids — pinned here with a stub that returns
    ``None`` for the ``/changes`` call.
    """

    def test_changes_endpoint_returning_none_exits_one_with_api_unavailable(self) -> None:
        runner = CliRunner()

        class _NullAPI:
            def get_json(self, endpoint: str) -> object:
                del endpoint
                return None

            def resolve_project(self, repo: str) -> object:
                del repo
                return None

            def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[JSONObject]:
                del project_id, mr_iid
                return []

            def get_draft_notes_count(self, project_id: int, mr_iid: int) -> int:
                del project_id, mr_iid
                return 0

            def get_mr_approvals(self, project_id: int, mr_iid: int) -> JSONObject:
                del project_id, mr_iid
                return {"count": 0, "required": 1, "approved_by": []}

        url = "https://gitlab.com/org/proj/-/merge_requests/77"
        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=_NullAPI()),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["error"] == "api_unavailable"
        assert payload["url"] == url
        # No verdict / changes fields — the audit must not synthesize fake "ready_to_review" output.
        assert "verdict" not in payload
        assert "changes" not in payload


class TestReviewRunLargeMRFinding:
    """Large MRs (>500 LOC of changes) surface a finding in the catalog."""

    def test_large_mr_emits_split_finding_and_needs_attention_verdict(self) -> None:
        runner = CliRunner()
        big_diff = _diff(added=600, removed=10)
        stub = _StubGitLabAPI(
            changes={
                "changes": [
                    {"new_path": "src/foo.py", "diff": big_diff},
                    {"new_path": "tests/test_foo.py", "diff": _diff(added=10, removed=0)},
                ],
            },
            discussions=[],
        )
        url = "https://gitlab.com/org/proj/-/merge_requests/99"

        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=stub),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["complexity"] == "large"
        assert payload["verdict"] == "needs_attention"
        assert any("large change" in finding for finding in payload["findings_catalog"]), payload


class TestReviewRunSkipsMergedClosed:
    """A merged/closed MR aborts the read-only audit with a skip verdict (#2081).

    The ``/changes`` payload carries the MR ``state`` field. When it is
    ``merged`` or ``closed`` a review note can never land, so the audit emits
    a ``skipped_merged`` / ``skipped_closed`` verdict instead of
    ``needs_attention`` / ``ready_to_review`` — a mid-flight close aborts the
    post rather than driving a doomed review.
    """

    def test_merged_mr_emits_skipped_merged_verdict(self) -> None:
        runner = CliRunner()
        stub = _StubGitLabAPI(
            changes={
                "state": "merged",
                "changes": [{"new_path": "src/foo.py", "diff": _diff(added=5, removed=2)}],
            },
            discussions=[{"notes": [{"resolved": False, "body": "still open"}]}],
        )
        url = "https://gitlab.com/org/proj/-/merge_requests/42"

        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=stub),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["verdict"] == "skipped_merged", payload

    def test_closed_mr_emits_skipped_closed_verdict(self) -> None:
        runner = CliRunner()
        stub = _StubGitLabAPI(
            changes={
                "state": "closed",
                "changes": [{"new_path": "src/foo.py", "diff": _diff(added=5, removed=2)}],
            },
            discussions=[],
        )
        url = "https://gitlab.com/org/proj/-/merge_requests/43"

        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=stub),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["verdict"] == "skipped_closed", payload

    def test_open_mr_keeps_normal_verdict(self) -> None:
        runner = CliRunner()
        stub = _StubGitLabAPI(
            changes={
                "state": "opened",
                "changes": [{"new_path": "src/foo.py", "diff": _diff(added=5, removed=2)}],
            },
            discussions=[{"notes": [{"resolved": False, "body": "still open"}]}],
        )
        url = "https://gitlab.com/org/proj/-/merge_requests/44"

        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=stub),
            patch(
                "teatree.cli.review.service.ReviewService.get_gitlab_token",
                return_value="t",
            ),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["verdict"] == "needs_attention", payload
