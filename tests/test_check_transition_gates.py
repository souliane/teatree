"""Tests for check_transition_gates.py."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from check_transition_gates import (
    _check_doing_to_review,
    _check_review_to_dev,
    check_gates,
)


class TestCheckDoingToReview:
    def test_no_mrs(self) -> None:
        result = _check_doing_to_review({}, {})
        assert not result["ready"]
        assert "No MRs found" in result["reason"]

    def test_all_reviewed(self) -> None:
        ticket = {"mrs": ["repo!1", "repo!2"]}
        mrs_data = {
            "repo!1": {"review_requested": True},
            "repo!2": {"review_requested": True},
        }
        result = _check_doing_to_review(ticket, mrs_data)
        assert result["ready"]
        assert "2/2" in result["reason"]

    def test_partial_review(self) -> None:
        ticket = {"mrs": ["repo!1", "repo!2"]}
        mrs_data = {
            "repo!1": {"review_requested": True},
            "repo!2": {},
        }
        result = _check_doing_to_review(ticket, mrs_data)
        assert not result["ready"]
        assert "1/2" in result["reason"]

    def test_skipped_mr(self) -> None:
        ticket = {"mrs": ["repo!1", "repo!2"]}
        mrs_data = {
            "repo!1": {"skipped": True, "skip_reason": "not needed"},
            "repo!2": {"review_requested": True},
        }
        result = _check_doing_to_review(ticket, mrs_data)
        assert result["ready"]
        assert "1/1" in result["reason"]
        assert any("skipped" in d for d in result["details"])

    def test_all_skipped(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        mrs_data = {"repo!1": {"skipped": True}}
        result = _check_doing_to_review(ticket, mrs_data)
        assert not result["ready"]
        assert "All MRs skipped" in result["reason"]

    def test_mr_not_in_data(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        result = _check_doing_to_review(ticket, {})
        assert not result["ready"]
        assert "NOT sent for review" in result["details"][0]


class TestCheckReviewToDev:
    def test_no_mrs(self) -> None:
        result = _check_review_to_dev({}, {})
        assert not result["ready"]

    def test_all_merged_no_extension(self) -> None:
        ticket = {"mrs": ["repo!1"], "_iid": "123"}
        mrs_data = {"repo!1": {"project_id": 42}}
        with (
            patch("check_transition_gates.get_mr_state", return_value={"state": "merged"}),
            patch.dict("sys.modules", {"lib.init": None}),
        ):
            result = _check_review_to_dev(ticket, mrs_data)
        # Extension point not available, so deployed = False
        assert not result["ready"]
        assert "not deployed" in result["reason"]

    def test_all_merged_and_deployed(self) -> None:
        ticket = {"mrs": ["repo!1"], "_iid": "123"}
        mrs_data = {"repo!1": {"project_id": 42}}
        with (
            patch("check_transition_gates.get_mr_state", return_value={"state": "merged"}),
            patch("check_transition_gates.get_mr", return_value={"id": 1}),
            patch.dict(
                "sys.modules",
                {
                    "lib.init": type("M", (), {"init": staticmethod(lambda: None)})(),
                    "lib.registry": type("M", (), {"call": staticmethod(lambda *_a, **_kw: True)})(),
                },
            ),
        ):
            result = _check_review_to_dev(ticket, mrs_data)
        assert result["ready"]
        assert "merged and deployed" in result["reason"]

    def test_not_merged(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        mrs_data = {"repo!1": {"project_id": 42}}
        with patch("check_transition_gates.get_mr_state", return_value={"state": "opened"}):
            result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "0/1" in result["reason"]

    def test_state_none(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        mrs_data = {"repo!1": {"project_id": 42}}
        with patch("check_transition_gates.get_mr_state", return_value=None):
            result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "unknown" in result["details"][0]

    def test_skipped_mr(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        mrs_data = {"repo!1": {"skipped": True}}
        result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "All MRs skipped" in result["reason"]

    def test_missing_project_id(self) -> None:
        ticket = {"mrs": ["repo!1"]}
        mrs_data = {"repo!1": {}}
        result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "missing project_id" in result["details"][0]

    def test_no_iid_in_key(self) -> None:
        ticket = {"mrs": ["repoX"]}
        mrs_data = {"repoX": {"project_id": 42}}
        result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "missing project_id" in result["details"][0]

    def test_extension_import_error(self) -> None:
        ticket = {"mrs": ["repo!1"], "_iid": "123"}
        mrs_data = {"repo!1": {"project_id": 42}}

        def raise_import(*_a: object, **_kw: object) -> None:
            msg = "no init"
            raise ImportError(msg)

        with (
            patch("check_transition_gates.get_mr_state", return_value={"state": "merged"}),
            patch.dict("sys.modules", {"lib.init": None}),
        ):
            result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]
        assert "not deployed" in result["reason"]
        assert any("extension point" in d for d in result["details"])

    def test_get_mr_returns_none_in_deploy_check(self) -> None:
        ticket = {"mrs": ["repo!1"], "_iid": "123"}
        mrs_data = {"repo!1": {"project_id": 42}}
        with (
            patch("check_transition_gates.get_mr_state", return_value={"state": "merged"}),
            patch("check_transition_gates.get_mr", return_value=None),
            patch.dict(
                "sys.modules",
                {
                    "lib.init": type("M", (), {"init": staticmethod(lambda: None)})(),
                    "lib.registry": type("M", (), {"call": staticmethod(lambda *_a, **_kw: False)})(),
                },
            ),
        ):
            result = _check_review_to_dev(ticket, mrs_data)
        assert not result["ready"]

    def test_skipped_mr_in_deploy_loop(self) -> None:
        """Cover branch 119->117: skipped MR is skipped during deploy check."""
        ticket = {"mrs": ["repo!1", "repo!2"], "_iid": "123"}
        mrs_data = {
            "repo!1": {"project_id": 42},
            "repo!2": {"project_id": 42, "skipped": True},
        }

        def mock_state(_pid: int, _iid: int, *_a: object, **_kw: object) -> dict:
            return {"state": "merged"}

        with (
            patch("check_transition_gates.get_mr_state", side_effect=mock_state),
            patch("check_transition_gates.get_mr", return_value={"id": 1}),
            patch.dict(
                "sys.modules",
                {
                    "lib.init": type("M", (), {"init": staticmethod(lambda: None)})(),
                    "lib.registry": type("M", (), {"call": staticmethod(lambda *_a, **_kw: True)})(),
                },
            ),
        ):
            result = _check_review_to_dev(ticket, mrs_data)
        assert result["ready"]


class TestCheckGates:
    def test_file_not_found(self) -> None:
        result = check_gates("/nonexistent/followup.json")
        assert "error" in result

    def test_doing_ticket(self, tmp_path: Path) -> None:
        data = {
            "tickets": {
                "123": {
                    "tracker_status": "Process::Doing",
                    "title": "Fix bug",
                    "mrs": ["repo!1"],
                },
            },
            "mrs": {"repo!1": {"review_requested": True}},
        }
        fp = tmp_path / "followup.json"
        fp.write_text(json.dumps(data))
        result = check_gates(str(fp))
        assert "123" in result
        assert result["123"]["target"] == "Process::Technical Review"

    def test_technical_review_ticket(self, tmp_path: Path) -> None:
        data = {
            "tickets": {
                "456": {
                    "tracker_status": "Process::Technical review",
                    "title": "Feature",
                    "mrs": ["repo!1"],
                },
            },
            "mrs": {"repo!1": {"project_id": 42}},
        }
        fp = tmp_path / "followup.json"
        fp.write_text(json.dumps(data))
        with patch("check_transition_gates.get_mr_state", return_value={"state": "opened"}):
            result = check_gates(str(fp))
        assert "456" in result
        assert result["456"]["target"] == "Process::DEV Review"

    def test_no_transitions(self, tmp_path: Path) -> None:
        data = {
            "tickets": {"789": {"tracker_status": "Process::Done", "title": "Done"}},
            "mrs": {},
        }
        fp = tmp_path / "followup.json"
        fp.write_text(json.dumps(data))
        result = check_gates(str(fp))
        assert result == {}

    def test_default_path(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        fp = data_dir / "followup.json"
        data = {"tickets": {}, "mrs": {}}
        fp.write_text(json.dumps(data))
        os.environ["T3_DATA_DIR"] = str(data_dir)
        try:
            result = check_gates()
            assert result == {}
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_empty_status(self, tmp_path: Path) -> None:
        data = {
            "tickets": {"1": {"tracker_status": "", "title": "X"}},
            "mrs": {},
        }
        fp = tmp_path / "followup.json"
        fp.write_text(json.dumps(data))
        result = check_gates(str(fp))
        assert result == {}

    def test_none_status(self, tmp_path: Path) -> None:
        data = {
            "tickets": {"1": {"tracker_status": None, "title": "X"}},
            "mrs": {},
        }
        fp = tmp_path / "followup.json"
        fp.write_text(json.dumps(data))
        result = check_gates(str(fp))
        assert result == {}
