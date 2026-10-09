"""``t3 <overlay> retro finding`` — the lane retro persists a lesson WITH.

A finding is recorded in the consolidation ledger and queued on the umbrella host's
pending ledger for the backlog sweep — the drain dreaming Pass 2 already uses. The
queueing is the default-OFF ``memory_promote`` mechanism, so the promoting cases opt in
and the OFF case is its own class.
"""

import json
import os
from io import StringIO
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory, Task, Ticket

pytestmark = pytest.mark.usefixtures("configured_banned_term_registry")

_UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
_RULE = "Run the tree-wide health gate before any push."
_CITATION = "pushed without running the gate, CI went red"
_DESTINATION = "skills/ship/SKILL.md"


class _RunsTheFindingCommand(TestCase):
    def _run(self, **kwargs: object) -> dict[str, object]:
        options: dict[str, object] = {"rule": _RULE, "citation": _CITATION, "destination": _DESTINATION, **kwargs}
        output = call_command("retro", "finding", stdout=StringIO(), **options)
        return cast("dict[str, object]", json.loads(output))

    @staticmethod
    def _pending() -> list[dict[str, str]]:
        return Ticket.objects.get(issue_url=_UMBRELLA).extra.get("dream_gap_pending", [])


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class RetroFindingCommandTest(_RunsTheFindingCommand):
    def setUp(self) -> None:
        Ticket.objects.create(issue_url=_UMBRELLA)

    def test_a_finding_is_queued_for_the_sweep_and_schedules_nothing(self) -> None:
        result = self._run()
        assert (result["queued"], result["withheld"], result["umbrella_url"]) == (True, False, _UMBRELLA)
        assert [entry["gap_key"] for entry in self._pending()] == [result["cluster_key"]]
        assert Task.objects.count() == 0
        assert ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"]).verified_citation == _CITATION

    def test_a_re_run_queues_nothing_twice(self) -> None:
        first = self._run()
        second = self._run()
        assert second["cluster_key"] == first["cluster_key"]
        assert second["queued"] is False
        assert len(self._pending()) == 1

    def test_a_dry_run_records_no_row_and_queues_nothing(self) -> None:
        result = self._run(dry_run=True)
        assert result["queued"] is False
        assert ConsolidatedMemory.objects.count() == 0
        assert self._pending() == []

    def test_a_banned_term_in_the_rule_is_withheld_and_nothing_is_queued(self) -> None:
        with patch("teatree.hooks.banned_terms_scanner.scan_text", return_value="acmecorp"):
            result = self._run(rule="Never name acmecorp in a public commit.")
        assert result["withheld"] is True
        assert self._pending() == []

    def test_a_missing_rule_is_a_refusal_not_a_silent_pass(self) -> None:
        result = self._run(rule="  ")
        assert "rule" in str(result["error"])
        assert ConsolidatedMemory.objects.count() == 0


class RetroFindingRefusalTest(_RunsTheFindingCommand):
    def test_a_destination_outside_a_teatree_fix_path_is_a_refusal(self) -> None:
        result = self._run(destination="~/notes/lessons.md")
        assert "destination" in str(result["error"])
        assert ConsolidatedMemory.objects.count() == 0

    def test_a_dry_run_refuses_that_destination_too(self) -> None:
        result = self._run(destination="~/notes/lessons.md", dry_run=True)
        assert "destination" in str(result["error"])


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "0"})
class RetroFindingPromoteToggleTest(_RunsTheFindingCommand):
    def test_the_toggle_off_records_the_finding_and_defers_the_queueing(self) -> None:
        Ticket.objects.create(issue_url=_UMBRELLA)
        result = self._run()
        assert (result["deferred"], result["queued"]) == (True, False)
        assert self._pending() == []
        assert ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"]).verified_citation == _CITATION


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class RetroFindingWithoutAnUmbrellaTicketTest(_RunsTheFindingCommand):
    def test_the_finding_is_recorded_and_stays_in_the_drain_queue(self) -> None:
        result = self._run()
        assert "error" not in result
        assert result["queued"] is False
        assert _UMBRELLA in str(result["reason"])
        row = ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"])
        assert list(ConsolidatedMemory.objects.needs_ticket()) == [row]
