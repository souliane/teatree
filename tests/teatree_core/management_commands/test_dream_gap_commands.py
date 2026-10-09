"""``t3 dream gap-coverage`` / ``gap-disposition`` — read-only proof and the per-gap record."""

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.models import ConsolidatedMemory, Ticket

UMBRELLA = "https://gitlab.com/o/factory/-/work_items/249"


class _UmbrellaSettingCase(TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "teatree.core.models.dream_gap_ledger.get_effective_settings",
            return_value=UserSettings(dream_umbrella_url=UMBRELLA),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


def _host(pk_hint: int, *keys: str, state: str = Ticket.State.WORK_STARTED) -> Ticket:
    return Ticket.objects.create(
        issue_url=f"https://gitlab.com/o/factory/-/work_items/{pk_hint}",
        state=state,
        extra={"dream_gap_batch": [{"gap_key": key, "cluster_key": key} for key in keys]},
    )


def _coverage(*args: str) -> tuple[int, dict[str, object]]:
    out = StringIO()
    try:
        call_command("dream", "gap-coverage", "--json", *args, stdout=out)
    except SystemExit as exc:
        return int(exc.code or 0), json.loads(out.getvalue())
    return 0, json.loads(out.getvalue())


class TestGapCoverage(_UmbrellaSettingCase):
    def test_every_gap_owned_once_exits_zero(self) -> None:
        _host(56, "a", "b")
        Ticket.objects.create(issue_url=UMBRELLA, extra={"dream_gap_pending": [{"gap_key": "c"}]})

        code, report = _coverage()

        assert (code, report["ok"], report["gaps"]) == (0, True, 3)

    def test_ticket_scope_passes_when_another_reconciled_host_dropped_a_gap(self) -> None:
        host = _host(56, "a")
        host.merge_extra(merge_into_dicts={"dream_gap_dispositions": {"a": {"disposition": "reject", "evidence": "x"}}})
        dropped = _host(57, "b", state=Ticket.State.MERGED)
        dropped.extra["dream_gap_reconciled_at"] = "2026-09-28T00:00:00Z"
        dropped.save(update_fields=["extra"])

        code, report = _coverage("--ticket", str(host.pk))

        assert (code, report["orphan"]) == (0, [])

    def test_ticket_scope_fails_when_this_host_dropped_a_gap(self) -> None:
        host = _host(56, "a", "b", state=Ticket.State.MERGED)
        host.extra["dream_gap_reconciled_at"] = "2026-09-28T00:00:00Z"
        host.extra["dream_gap_dispositions"] = {"a": {"disposition": "reject", "evidence": "x"}}
        host.save(update_fields=["extra"])
        other = _host(57, "c", state=Ticket.State.MERGED)
        other.extra["dream_gap_reconciled_at"] = "2026-09-28T00:00:00Z"
        other.save(update_fields=["extra"])

        scoped_code, scoped = _coverage("--ticket", str(host.pk))
        global_code, global_report = _coverage()

        assert (scoped_code, scoped["orphan"]) == (1, ["b"])
        assert (global_code, global_report["orphan"]) == (1, ["b", "c"])

    def test_a_gap_owned_twice_is_a_duplicate(self) -> None:
        first, second = _host(56, "a"), _host(110, "a")
        umbrella = Ticket.objects.create(issue_url=UMBRELLA, extra={"dream_gap_pending": [{"gap_key": "b"}]})
        folded = _host(107, "b")

        code, report = _coverage()

        assert code == 1
        assert report["duplicate"] == {"a": [first.pk, second.pk], "b": [umbrella.pk, folded.pk]}

    def test_a_retired_ticket_still_carrying_a_batch_is_reported(self) -> None:
        retired = _host(897, "a", state=Ticket.State.IGNORED)

        code, report = _coverage()

        assert (code, report["retired_owner"]) == (1, [retired.pk])

    def test_a_named_host_with_an_undispositioned_gap_exits_one(self) -> None:
        host = _host(56, "a", "b")
        call_command("dream", "gap-disposition", str(host.pk), "a", "--reject", "not a core gap", stdout=StringIO())

        code, report = _coverage("--ticket", str(host.pk))

        assert (code, report["undispositioned"]) == (1, ["b"])

    def test_a_named_host_with_every_gap_dispositioned_exits_zero(self) -> None:
        host = _host(56, "a")
        ConsolidatedMemory.objects.create(
            cluster_key="a", rule="r", source_files=[], member_count=1, max_member_weight=90
        )
        call_command("dream", "gap-disposition", str(host.pk), "a", "--citation", "task 5561", stdout=StringIO())

        assert _coverage("--ticket", str(host.pk))[0] == 0

    def test_it_writes_nothing(self) -> None:
        host = _host(56, "a")
        before = Ticket.objects.get(pk=host.pk).extra

        _coverage("--ticket", str(host.pk))

        assert Ticket.objects.get(pk=host.pk).extra == before


class TestGapDisposition(_UmbrellaSettingCase):
    def test_a_disposition_without_exactly_one_of_citation_or_reject_exits_one(self) -> None:
        host = _host(56, "a")
        with pytest.raises(SystemExit) as exc:
            call_command("dream", "gap-disposition", str(host.pk), "a", stdout=StringIO(), stderr=StringIO())
        assert exc.value.code == 1

    def test_an_unknown_ticket_exits_one(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("dream", "gap-disposition", "9999", "a", "--reject", "x", stdout=StringIO(), stderr=StringIO())
        assert exc.value.code == 1
