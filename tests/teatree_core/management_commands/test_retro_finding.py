"""``t3 <overlay> retro finding`` — the lane retro persists a lesson WITH.

A finding is recorded in the consolidation ledger and driven onto the standing
umbrella as a deduped checkbox plus a scheduled coding task — the drain dreaming
Pass 2 already uses. The code host is injected as a fake, so the whole lane runs
without a live forge. The umbrella write is the default-OFF ``memory_promote``
mechanism, so the promoting cases opt in and the OFF case is its own class.
"""

import json
import os
from io import StringIO
from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.task import Task
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}

_RULE = "Run the tree-wide health gate before any push."
_CITATION = "pushed without running the gate, CI went red"
_DESTINATION = "skills/ship/SKILL.md"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    return host


class _RunsTheFindingCommand(TestCase):
    def _run(self, host: CodeHostBackend, **kwargs: object) -> dict[str, object]:
        options: dict[str, object] = {
            "rule": _RULE,
            "citation": _CITATION,
            "destination": _DESTINATION,
            **kwargs,
        }
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            output = call_command("retro", "finding", stdout=StringIO(), **options)
        return cast("dict[str, object]", json.loads(output))


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class RetroFindingCommandTest(_RunsTheFindingCommand):
    def test_a_finding_rides_the_umbrella_and_schedules_one_coding_fix(self) -> None:
        result = self._run(_fake_host())
        assert result["checkbox_added"] is True
        assert result["scheduled"] is True
        assert result["withheld"] is False
        assert Task.objects.count() == 1
        assert ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"]).verified_citation == _CITATION

    def test_a_re_run_adds_no_second_checkbox_and_schedules_no_second_task(self) -> None:
        first = self._run(_fake_host())
        already = f"## Open gaps\n- [ ] anything <!-- dream-gap {first['cluster_key']} -->\n"
        second = self._run(_fake_host(body=already))
        assert second["cluster_key"] == first["cluster_key"]
        assert second["checkbox_added"] is False
        assert second["scheduled"] is False
        assert ConsolidatedMemory.objects.count() == 1
        assert Task.objects.count() == 1

    def test_a_dry_run_records_no_row_writes_nothing_and_schedules_nothing(self) -> None:
        host = _fake_host()
        result = self._run(host, dry_run=True)
        host.update_issue.assert_not_called()
        assert (result["checkbox_added"], result["scheduled"]) == (False, False)
        assert ConsolidatedMemory.objects.count() == 0
        assert Task.objects.count() == 0

    def test_a_banned_term_in_the_rule_is_withheld_and_nothing_is_written(self) -> None:
        host = _fake_host()
        with patch("teatree.hooks.banned_terms_scanner.scan_text", return_value="acmecorp"):
            result = self._run(host, rule="Never name acmecorp in a public commit.")
        assert result["withheld"] is True
        host.update_issue.assert_not_called()
        assert Task.objects.count() == 0

    def test_a_missing_rule_is_a_refusal_not_a_silent_pass(self) -> None:
        result = self._run(_fake_host(), rule="  ")
        assert "rule" in str(result["error"])
        assert ConsolidatedMemory.objects.count() == 0


class RetroFindingRefusalTest(_RunsTheFindingCommand):
    def test_a_destination_outside_a_teatree_fix_path_is_a_refusal(self) -> None:
        result = self._run(_fake_host(), destination="~/notes/lessons.md")
        assert "destination" in str(result["error"])
        assert ConsolidatedMemory.objects.count() == 0

    def test_a_dry_run_refuses_that_destination_too(self) -> None:
        result = self._run(_fake_host(), destination="~/notes/lessons.md", dry_run=True)
        assert "destination" in str(result["error"])


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "0"})
class RetroFindingPromoteToggleTest(_RunsTheFindingCommand):
    def test_the_toggle_off_records_the_finding_and_defers_the_umbrella_write(self) -> None:
        host = _fake_host()
        result = self._run(host)
        assert result["deferred"] is True
        assert result["checkbox_added"] is False
        host.update_issue.assert_not_called()
        assert Task.objects.count() == 0
        assert ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"]).verified_citation == _CITATION


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class RetroFindingUnreachableHostTest(_RunsTheFindingCommand):
    def test_an_unresolvable_host_records_the_finding_and_defers_the_umbrella_write(self) -> None:
        result = self._run(cast("CodeHostBackend", None))
        assert "error" not in result
        assert result["deferred"] is True
        assert result["checkbox_added"] is False
        assert Task.objects.count() == 0
        assert ConsolidatedMemory.objects.get(cluster_key=result["cluster_key"]).verified_citation == _CITATION
