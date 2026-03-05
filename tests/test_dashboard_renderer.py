"""Tests for lib.dashboard_renderer and generate_dashboard CLI."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from generate_dashboard import main as cli_main
from lib.dashboard_renderer import (
    _approval_pill,
    _collect_statuses,
    _e2e_cell,
    _extract_feature_flag,
    _format_time_ago,
    _pipeline_pill,
    _review_request_cell,
    _status_pill,
    render_dashboard,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_data() -> dict:
    return {
        "generated_at": "2026-03-10T06:00:00Z",
        "tickets": {
            "100": {
                "title": "Add postal code validation",
                "url": "https://example.com/issues/100",
                "gitlab_status": "Process::Doing",
                "mrs": ["backend!101"],
            }
        },
        "mrs": {
            "backend!101": {
                "url": "https://example.com/merge_requests/101",
                "repo": "backend",
                "project_id": 123,
                "title": "feat: add postal code [postal_code_v2]",
                "branch": "ac-backend-100-postal-code",
                "ticket": "100",
                "pipeline_status": "success",
                "pipeline_url": "https://example.com/pipelines/999",
                "review_requested": True,
                "review_channel": "#backend-review",
                "review_permalink": "https://chat.example.com/msg/123",
                "review_comments": {"status": "addressed", "details": "All fixed"},
                "e2e_test_plan_url": None,
                "approvals": {"count": 1, "required": 1},
            }
        },
        "review_comments_tracking": {},
        "actions_log": ["Pushed fix for postal code"],
    }


@pytest.fixture
def multi_mr_data() -> dict:
    return {
        "generated_at": "2026-03-10T06:00:00Z",
        "tickets": {
            "200": {
                "title": "Community property",
                "url": "https://example.com/issues/200",
                "gitlab_status": "Process::Doing",
                "notion_status": "In Progress (dev/config)",
                "mrs": ["backend!201", "frontend!202"],
            }
        },
        "mrs": {
            "backend!201": {
                "url": "https://example.com/mr/201",
                "repo": "backend",
                "title": "feat: community property [community_prop]",
                "ticket": "200",
                "pipeline_status": "success",
                "pipeline_url": None,
                "review_requested": True,
                "review_channel": "#backend-review",
                "review_permalink": None,
                "review_comments": None,
                "approvals": {"count": 0, "required": 1},
            },
            "frontend!202": {
                "url": "https://example.com/mr/202",
                "repo": "frontend",
                "title": "feat: community property form",
                "ticket": "200",
                "pipeline_status": "running",
                "pipeline_url": None,
                "review_requested": False,
                "review_channel": "#frontend-review",
                "review_permalink": None,
                "review_comments": None,
                "e2e_test_plan_url": "https://example.com/mr/202#note_123",
                "approvals": {"count": 0, "required": 1},
            },
        },
        "review_comments_tracking": {
            "old-repo!50": {
                "url": "https://example.com/mr/50",
                "status": "waiting_reviewer",
                "details": "Waiting for reviewer decision.",
            }
        },
        "actions_log": [],
    }


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestFormatTimeAgo:
    def test_just_now(self) -> None:
        now = datetime.now(UTC).isoformat()
        assert _format_time_ago(now) == "just now"

    def test_minutes_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        assert "m ago" in _format_time_ago(ts)

    def test_hours_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(hours=3, minutes=25)).isoformat()
        result = _format_time_ago(ts)
        assert "3h" in result
        assert "25m" in result

    def test_exact_hours(self) -> None:
        ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        assert _format_time_ago(ts) == "2h ago"

    def test_invalid_timestamp(self) -> None:
        assert _format_time_ago("not-a-date") == ""

    def test_z_suffix(self) -> None:
        ts = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _format_time_ago(ts)
        assert "m ago" in result

    def test_empty_string(self) -> None:
        assert _format_time_ago("") == ""


class TestPipelinePill:
    def test_success(self) -> None:
        result = _pipeline_pill("success", "https://example.com/pipeline")
        assert "success" in result
        assert "href" in result

    def test_failed(self) -> None:
        result = _pipeline_pill("failed", "https://example.com/pipeline")
        assert "failed" in result

    def test_running(self) -> None:
        result = _pipeline_pill("running", None)
        assert "running" in result

    def test_pending(self) -> None:
        result = _pipeline_pill("pending", "")
        assert "pending" in result

    def test_none_status(self) -> None:
        assert _pipeline_pill(None, None) == "&mdash;"

    def test_skip_reason_with_failed(self) -> None:
        result = _pipeline_pill("failed", "https://url", "lint + sonarqube")
        assert "lint + sonarqube" in result
        assert "failed" in result

    def test_skip_reason_without_failed(self) -> None:
        result = _pipeline_pill("success", "", "ruff MR group")
        assert "skipped" in result

    def test_unknown_status(self) -> None:
        result = _pipeline_pill("canceled", None)
        assert "canceled" in result
        assert "pending" in result  # css class


class TestStatusPill:
    def test_doing(self) -> None:
        result = _status_pill("Process::Doing", "https://example.com")
        assert "running" in result
        assert "Doing" in result
        assert "href" in result

    def test_technical_review(self) -> None:
        result = _status_pill("Technical Review")
        assert "success" in result

    def test_unknown(self) -> None:
        result = _status_pill("SomeNewStatus")
        assert "running" in result
        assert "SomeNewStatus" in result


class TestExtractFeatureFlag:
    def test_from_ticket(self) -> None:
        ticket = {"feature_flag": "my_flag"}
        assert _extract_feature_flag(ticket, {}, []) == "my_flag"

    def test_from_mr_title(self) -> None:
        ticket: dict = {}
        mrs = {"repo!1": {"title": "feat: add field [postal_code_v2]"}}
        assert _extract_feature_flag(ticket, mrs, ["repo!1"]) == "postal_code_v2"

    def test_none_flag_ignored(self) -> None:
        ticket: dict = {}
        mrs = {"repo!1": {"title": "fix: something [none]"}}
        assert _extract_feature_flag(ticket, mrs, ["repo!1"]) == ""

    def test_no_flag(self) -> None:
        ticket: dict = {}
        mrs = {"repo!1": {"title": "fix: something"}}
        assert _extract_feature_flag(ticket, mrs, ["repo!1"]) == ""

    def test_ticket_flag_takes_priority(self) -> None:
        ticket = {"feature_flag": "from_ticket"}
        mrs = {"repo!1": {"title": "feat: x [from_mr]"}}
        assert _extract_feature_flag(ticket, mrs, ["repo!1"]) == "from_ticket"


class TestApprovalPill:
    def test_approved(self) -> None:
        result = _approval_pill({"approvals": {"count": 2, "required": 2}})
        assert "approved" in result
        assert "2/2" in result

    def test_pending(self) -> None:
        result = _approval_pill({"approvals": {"count": 0, "required": 1}})
        assert "pending" in result
        assert "0/1" in result

    def test_no_approvals_key(self) -> None:
        result = _approval_pill({})
        assert "0/1" in result


class TestReviewRequestCell:
    def test_skipped(self) -> None:
        result = _review_request_cell({"skipped": True})
        assert "skipped" in result

    def test_not_requested(self) -> None:
        result = _review_request_cell({"review_requested": False})
        assert "not sent" in result

    def test_requested_with_permalink(self) -> None:
        result = _review_request_cell(
            {
                "review_requested": True,
                "review_channel": "#review",
                "review_permalink": "https://chat/msg",
            }
        )
        assert "#review" in result
        assert "success" in result

    def test_requested_waiting_pipeline(self) -> None:
        result = _review_request_cell(
            {
                "review_requested": True,
                "review_channel": "#review",
                "pipeline_status": "running",
            }
        )
        assert "waiting pipeline" in result

    def test_requested_waiting_group(self) -> None:
        result = _review_request_cell(
            {
                "review_requested": True,
                "review_channel": "#review",
                "pipeline_status": "success",
            }
        )
        assert "waiting group" in result


class TestE2eCell:
    def test_with_url(self) -> None:
        result = _e2e_cell({"e2e_test_plan_url": "https://example.com/note"})
        assert "test plan" in result
        assert "href" in result

    def test_without_url(self) -> None:
        assert _e2e_cell({}) == "&mdash;"


class TestCollectStatuses:
    def test_tracker_status_only(self) -> None:
        assert _collect_statuses({"tracker_status": "Doing"}) == ["Doing"]

    def test_gitlab_and_notion(self) -> None:
        ticket = {"gitlab_status": "Process::Doing", "notion_status": "In Progress"}
        result = _collect_statuses(ticket)
        assert result == ["Process::Doing", "In Progress"]

    def test_no_duplicates(self) -> None:
        ticket = {"tracker_status": "Doing", "gitlab_status": "Doing"}
        assert _collect_statuses(ticket) == ["Doing"]

    def test_empty(self) -> None:
        assert _collect_statuses({}) == []


# ---------------------------------------------------------------------------
# Integration tests — render_dashboard
# ---------------------------------------------------------------------------


class TestRenderDashboard:
    def test_minimal(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert "<!DOCTYPE html>" in html
        assert "t3-followup Dashboard" in html
        assert "In-Flight Work" in html
        assert "#100" in html
        assert "postal code" in html.lower()
        assert "postal_code_v2" in html
        assert "backend !101" in html
        assert "success" in html
        assert "Actions Taken" in html
        assert "Pushed fix" in html

    def test_multi_mr_rowspan(self, multi_mr_data: dict) -> None:
        html = render_dashboard(multi_mr_data)
        assert 'rowspan="2"' in html
        assert "backend !201" in html
        assert "frontend !202" in html

    def test_stacked_statuses(self, multi_mr_data: dict) -> None:
        html = render_dashboard(multi_mr_data)
        assert "status-stack" in html
        assert "Doing" in html
        assert "In Progress" in html

    def test_review_comments_section(self, multi_mr_data: dict) -> None:
        html = render_dashboard(multi_mr_data)
        assert "Review Comments" in html
        assert "old-repo !50" in html
        assert "Waiting reviewer" in html

    def test_review_comments_from_mrs(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert "Review Comments" in html
        assert "Addressed" in html

    def test_draft_mrs_section(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {},
            "mrs": {},
            "draft_mrs": {
                "repo!99": {
                    "url": "https://example.com/mr/99",
                    "repo": "repo",
                    "title": "WIP: improve something",
                    "pipeline_status": None,
                    "pipeline_url": None,
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "Draft MRs" in html
        assert "repo !99" in html
        assert "improve something" in html

    def test_no_draft_mrs_section_when_empty(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert "Draft MRs" not in html

    def test_no_actions_section_when_empty(self, multi_mr_data: dict) -> None:
        html = render_dashboard(multi_mr_data)
        assert "Actions Taken" not in html

    def test_empty_data(self) -> None:
        html = render_dashboard({})
        assert "<!DOCTYPE html>" in html
        assert "In-Flight Work" not in html

    def test_generated_at_display(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert "2026-03-10 06:00 UTC" in html

    def test_time_ago_in_meta(self) -> None:
        recent = datetime.now(UTC).isoformat()
        data: dict = {"generated_at": recent, "tickets": {}, "mrs": {}, "actions_log": []}
        html = render_dashboard(data)
        assert "just now" in html

    def test_meta_refresh(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert 'http-equiv="refresh" content="120"' in html

    def test_css_included(self, minimal_data: dict) -> None:
        html = render_dashboard(minimal_data)
        assert "--bg: #1a1b26" in html
        assert "Tokyo Night" not in html  # palette name not in output

    def test_misc_ticket_no_url(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {
                "misc-99": {
                    "title": "Quick fix",
                    "url": None,
                    "mrs": ["repo!10"],
                }
            },
            "mrs": {
                "repo!10": {
                    "url": "https://example.com/mr/10",
                    "repo": "repo",
                    "title": "fix: quick fix",
                    "ticket": "misc-99",
                    "pipeline_status": "success",
                    "pipeline_url": None,
                    "review_requested": False,
                    "review_channel": "#review",
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "misc" in html
        assert "Quick fix" in html

    def test_skipped_mr(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {
                "300": {
                    "title": "Adopt ruff",
                    "url": "https://example.com/issues/300",
                    "mrs": ["repo!30"],
                }
            },
            "mrs": {
                "repo!30": {
                    "url": "https://example.com/mr/30",
                    "repo": "repo",
                    "title": "techdebt: ruff",
                    "ticket": "300",
                    "pipeline_status": "failed",
                    "pipeline_url": None,
                    "review_requested": False,
                    "review_channel": "#review",
                    "skipped": True,
                    "skip_reason": "lint + sonarqube",
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "skipped" in html
        assert "lint + sonarqube" in html

    def test_ticket_with_no_matching_mrs(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {"400": {"title": "Orphan", "url": None, "mrs": ["missing!1"]}},
            "mrs": {},
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "Orphan" not in html

    def test_e2e_link_rendered(self, multi_mr_data: dict) -> None:
        html = render_dashboard(multi_mr_data)
        assert "test plan" in html
        assert "note_123" in html

    def test_pipeline_url_fallback(self) -> None:
        """When pipeline_url is None, falls back to MR URL + /pipelines."""
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {"1": {"title": "T", "url": None, "mrs": ["r!1"]}},
            "mrs": {
                "r!1": {
                    "url": "https://example.com/mr/1",
                    "repo": "r",
                    "title": "fix",
                    "ticket": "1",
                    "pipeline_status": "success",
                    "pipeline_url": None,
                    "review_requested": False,
                    "review_channel": "#x",
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "example.com/mr/1/pipelines" in html

    def test_draft_mr_pipeline_url_fallback(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {},
            "mrs": {},
            "draft_mrs": {
                "repo!5": {
                    "url": "https://example.com/mr/5",
                    "repo": "repo",
                    "title": "Draft thing",
                    "pipeline_status": "running",
                    "pipeline_url": None,
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "example.com/mr/5/pipelines" in html

    def test_draft_mr_explicit_pipeline_url(self) -> None:
        data = {
            "generated_at": "2026-01-01T00:00:00Z",
            "tickets": {},
            "mrs": {},
            "draft_mrs": {
                "repo!6": {
                    "url": "https://example.com/mr/6",
                    "repo": "repo",
                    "title": "Draft with pipeline",
                    "pipeline_status": "success",
                    "pipeline_url": "https://example.com/pipelines/42",
                }
            },
            "actions_log": [],
        }
        html = render_dashboard(data)
        assert "example.com/pipelines/42" in html
        assert "example.com/mr/6/pipelines" not in html


# ---------------------------------------------------------------------------
# Golden test — render_dashboard vs stored reference HTML
# ---------------------------------------------------------------------------

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# Comprehensive golden input covering most renderer branches:
# - Ticket with URL + single MR (success pipeline, approved, review permalink, feature flag from MR title)
# - Ticket with URL + stacked statuses (gitlab_status + notion_status) + two MRs (rowspan):
#   * MR with explicit pipeline URL, e2e test plan, review requested but waiting pipeline
#   * Skipped MR with skip_reason (failed pipeline + skip_reason branch)
# - Misc ticket (no URL, ticket_id starts with "misc") + MR with no pipeline (null status)
# - review_comments_tracking entries: waiting_reviewer + needs_reply statuses
# - Review comments from MR (addressed — merged into review section)
# - Draft MRs: one with explicit pipeline URL, one with pipeline fallback to mr_url/pipelines
# - Actions log with multiple entries
# - Unknown pipeline status (canceled) for coverage of fallback CSS
_GOLDEN_INPUT = {
    "generated_at": "2026-03-10T06:00:00+00:00",
    "tickets": {
        "100": {
            "title": "Add postal code validation",
            "url": "https://example.com/issues/100",
            "tracker_status": "Process::Doing",
            "mrs": ["backend!101"],
        },
        "200": {
            "title": "Community property support",
            "url": "https://example.com/issues/200",
            "gitlab_status": "Process::Technical Review",
            "notion_status": "In Progress (dev/config)",
            "feature_flag": "community_prop",
            "mrs": ["backend!201", "frontend!202"],
        },
        "misc-1": {
            "title": "Quick hotfix",
            "url": None,
            "mrs": ["backend!301"],
        },
    },
    "mrs": {
        "backend!101": {
            "url": "https://example.com/merge_requests/101",
            "repo": "backend",
            "project_id": 123,
            "title": "feat: add postal code [postal_code_v2]",
            "branch": "ac-backend-100-postal-code",
            "ticket": "100",
            "pipeline_status": "success",
            "pipeline_url": "https://example.com/pipelines/999",
            "review_requested": True,
            "review_channel": "#backend-review",
            "review_permalink": "https://chat.example.com/msg/123",
            "review_comments": {"status": "addressed", "details": "All fixed"},
            "e2e_test_plan_url": None,
            "approvals": {"count": 1, "required": 1},
        },
        "backend!201": {
            "url": "https://example.com/merge_requests/201",
            "repo": "backend",
            "project_id": 123,
            "title": "feat: community property backend",
            "ticket": "200",
            "pipeline_status": "running",
            "pipeline_url": "https://example.com/pipelines/1001",
            "review_requested": True,
            "review_channel": "#backend-review",
            "review_permalink": None,
            "review_comments": None,
            "e2e_test_plan_url": "https://example.com/merge_requests/201#note_55",
            "approvals": {"count": 0, "required": 2},
        },
        "frontend!202": {
            "url": "https://example.com/merge_requests/202",
            "repo": "frontend",
            "project_id": 456,
            "title": "feat: community property form [community_prop]",
            "ticket": "200",
            "pipeline_status": "failed",
            "pipeline_url": None,
            "review_requested": False,
            "review_channel": "#frontend-review",
            "review_permalink": None,
            "review_comments": {"status": "pending", "details": "Waiting for author"},
            "e2e_test_plan_url": None,
            "skipped": True,
            "skip_reason": "lint + sonarqube",
            "approvals": {"count": 0, "required": 1},
        },
        "backend!301": {
            "url": "https://example.com/merge_requests/301",
            "repo": "backend",
            "project_id": 123,
            "title": "fix: hotfix",
            "ticket": "misc-1",
            "pipeline_status": "canceled",
            "pipeline_url": None,
            "review_requested": False,
            "review_channel": "#backend-review",
            "review_comments": None,
            "approvals": None,
        },
    },
    "review_comments_tracking": {
        "old-repo!50": {
            "url": "https://example.com/merge_requests/50",
            "status": "waiting_reviewer",
            "details": "Waiting for reviewer decision.",
        },
        "old-repo!51": {
            "url": "https://example.com/merge_requests/51",
            "status": "needs_reply",
            "details": "Author needs to respond.",
        },
    },
    "draft_mrs": {
        "backend!401": {
            "url": "https://example.com/merge_requests/401",
            "repo": "backend",
            "title": "WIP: refactor auth module",
            "pipeline_status": "success",
            "pipeline_url": "https://example.com/pipelines/2000",
        },
        "frontend!402": {
            "url": "https://example.com/merge_requests/402",
            "repo": "frontend",
            "title": "WIP: new settings page",
            "pipeline_status": "pending",
            "pipeline_url": None,
        },
    },
    "actions_log": [
        "Pushed fix for postal code",
        "Transitioned #200 to Technical Review",
        "Sent reminder for backend!101",
    ],
}


class TestGoldenDashboard:
    def test_matches_golden_html(self) -> None:
        """Render dashboard from known input and compare to golden file.

        If the renderer changes intentionally, regenerate the golden file:
            PYENV_VERSION=3.12.6 python3 -c "
            import sys; sys.path.insert(0, 'scripts')
            from lib.dashboard_renderer import render_dashboard
            from tests.test_dashboard_renderer import _GOLDEN_INPUT
            print(render_dashboard(_GOLDEN_INPUT), end='')
            " > tests/assets/golden_dashboard.html
        """
        golden_path = _ASSETS_DIR / "golden_dashboard.html"
        assert golden_path.is_file(), f"Golden file not found: {golden_path}"
        golden = golden_path.read_text(encoding="utf-8")
        actual = render_dashboard(_GOLDEN_INPUT)
        # The golden file was generated with a fixed timestamp, so the "time ago"
        # part will drift. Strip it for comparison.
        time_ago_re = re.compile(r"\(\d+h?\s*\d*m?\s*ago\)")
        golden_normalized = time_ago_re.sub("(TIME_AGO)", golden)
        actual_normalized = time_ago_re.sub("(TIME_AGO)", actual)
        assert actual_normalized == golden_normalized


# ---------------------------------------------------------------------------
# CLI script tests
# ---------------------------------------------------------------------------


class TestGenerateDashboardCli:
    def test_generates_html_file(self, tmp_path: Path, minimal_data: dict) -> None:
        input_file = tmp_path / "followup.json"
        output_file = tmp_path / "followup.html"
        input_file.write_text(json.dumps(minimal_data), encoding="utf-8")

        cli_main(input_path=input_file, output_path=output_file)

        assert output_file.is_file()
        html = output_file.read_text(encoding="utf-8")
        assert "In-Flight Work" in html

    def test_missing_input_file(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="1"):
            cli_main(input_path=tmp_path / "missing.json")

    def test_default_paths_from_env(self, tmp_path: Path, minimal_data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "followup.json").write_text(json.dumps(minimal_data), encoding="utf-8")
        monkeypatch.setenv("T3_DATA_DIR", str(data_dir))

        cli_main(input_path=None, output_path=None)

        assert (data_dir / "followup.html").is_file()
