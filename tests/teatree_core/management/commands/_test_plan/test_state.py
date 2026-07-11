"""Tests for the persisted test-plan state model (``_test_plan/state.py``).

Pure transforms over the hidden ``t3-e2e-data`` blob: defensive coercion of a
JSON payload into a typed :class:`TestPlanState`, the note-marker parse/emit,
and the blob round-trip. No ORM, no code host — every case is a value-in /
value-out assertion on the state layer split out of ``render.py`` (Unit 22).
"""

import json

from teatree.core.management.commands._test_plan import state as state_mod
from teatree.core.management.commands._test_plan.state import coerce_state, empty_state, parse_state_blob


class TestEmptyState:
    def test_both_sides_empty_and_dev_carries_gap_key(self) -> None:
        state = empty_state(ticket="8521", title="Feature X")
        assert state["ticket"] == "8521"
        assert state["title"] == "Feature X"
        assert state["mrs"] == []
        assert state["dev"] == {"commits": {}, "missing_on_dev": [], "workflows": {}}
        assert state["local"] == {"commits": {}, "workflows": {}}
        assert state["steps"] == {}


class TestCoerceState:
    def test_drops_malformed_fields_and_keeps_valid_ones(self) -> None:
        raw = {
            "ticket": 8521,  # non-str coerced
            "title": "T",
            "mrs": ["a", 2, None],  # each coerced to str
            "dev": {"commits": {"repo": "abc"}, "missing_on_dev": ["repo2"], "workflows": {}},
            "local": {"commits": {}, "workflows": {}},
            "steps": {"wf": ["step1", "step2"], "empty": []},  # empty dropped
            "template": "capture-matrix",
            "blocked_workflows": {"wf": "reason", "": "no name", "wf2": ""},  # last two dropped
        }
        state = coerce_state(raw)
        assert state["ticket"] == "8521"
        assert state["mrs"] == ["a", "2", "None"]
        assert state["dev"]["commits"] == {"repo": "abc"}
        assert state["dev"]["missing_on_dev"] == ["repo2"]
        assert state["steps"] == {"wf": ["step1", "step2"]}
        assert state["template"] == "capture-matrix"
        assert state["blocked_workflows"] == {"wf": "reason"}

    def test_unknown_template_is_dropped(self) -> None:
        state = coerce_state({"ticket": "1", "template": "not-a-template"})
        assert "template" not in state

    def test_non_dict_input_yields_all_empty(self) -> None:
        state = coerce_state("not a dict")
        assert state["ticket"] == ""
        assert state["mrs"] == []
        assert state["steps"] == {}


class TestParseStateBlob:
    def test_round_trips_a_valid_blob(self) -> None:
        original = empty_state(ticket="42", title="Round trip")
        original["mrs"] = ["https://example.com/x/-/merge_requests/1"]
        body = f"prose\n<!-- t3-e2e-data {json.dumps(original, separators=(',', ':'), sort_keys=True)} -->\nmore"
        recovered = parse_state_blob(body)
        assert recovered["ticket"] == "42"
        assert recovered["title"] == "Round trip"
        assert recovered["mrs"] == ["https://example.com/x/-/merge_requests/1"]

    def test_missing_blob_returns_empty_state(self) -> None:
        assert parse_state_blob("no blob here") == empty_state(ticket="", title="")

    def test_corrupt_json_returns_empty_state(self) -> None:
        assert parse_state_blob("<!-- t3-e2e-data {not json} -->") == empty_state(ticket="", title="")


class TestMarkers:
    def test_marker_emit_and_find_are_consistent(self) -> None:
        marker = state_mod.test_plan_marker(ticket_id="8521")
        assert state_mod.find_ticket_marker(f"body\n{marker}\ntail", ticket_id="8521") is True

    def test_find_rejects_a_different_ticket(self) -> None:
        marker = state_mod.test_plan_marker(ticket_id="8521")
        assert state_mod.find_ticket_marker(marker, ticket_id="9999") is False

    def test_find_is_false_without_a_marker(self) -> None:
        assert state_mod.find_ticket_marker("no marker", ticket_id="8521") is False
