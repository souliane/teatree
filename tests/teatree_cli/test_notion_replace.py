"""``t3 notion replace`` — an anchored, block-scoped text replace on an internal page."""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer.testing
from django.test import TestCase

from teatree.cli.notion import notion_app
from teatree.core.models import OutboundClaim
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_ROOTS = "teatree.backends.notion.write_guard.notion_write_roots"
_ELSEWHERE = "99999999-9999-9999-9999-999999999999"


def _run(text: str, *, bold: bool = False, url: str = "") -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": text, "link": {"url": url} if url else None},
        "plain_text": text,
        "href": url or None,
        "annotations": {"bold": bold, "italic": False, "code": False, "color": "default"},
    }


def _para_payload(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [_run(text)]}}


def _paragraph(notion: FakeNotion, *runs: dict[str, Any], parent: str = "") -> str:
    return notion.add({"type": "paragraph", "paragraph": {"rich_text": list(runs)}}, parent=parent)


def _runs(notion: FakeNotion, block_id: str) -> list[dict[str, Any]]:
    return notion.blocks[block_id]["paragraph"]["rich_text"]


def _patches(notion: FakeNotion) -> list[tuple[str, str]]:
    return [request for request in notion.requests if request[0] == "PATCH"]


class ReplaceCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NOTION_TOKEN", "test-token")
        self.notion = install_fake_notion(monkeypatch)
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.runner = typer.testing.CliRunner()

    def replace(self, old: str, new: str, *extra: str) -> typer.testing.Result:
        old_file, new_file = self.tmp_path / "old.txt", self.tmp_path / "new.txt"
        old_file.write_text(f"{old}\n", encoding="utf-8")
        new_file.write_text(f"{new}\n", encoding="utf-8")
        args = ["replace", self.notion.page_id, "--old-file", str(old_file), "--new-file", str(new_file), *extra]
        return self.runner.invoke(notion_app, args)


class TestUniqueAnchor(ReplaceCase):
    def test_the_single_occurrence_is_replaced_verified_and_audited(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 0, result.output
        assert "writing as integration 'Factory' (bot id bot-1)" in result.stderr
        assert json.loads(result.stdout)["block_id"] == block
        assert self.notion.text_of(block) == "Rates reset every month."
        claim = OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT)
        assert claim.verified_at is not None
        assert claim.extra["block_id"] == block
        assert claim.extra["old_sha256"] == hashlib.sha256(b"every quarter").hexdigest()
        assert claim.extra["new_length"] == len("every month")
        assert claim.extra["actor"] == "Factory (bot-1)"
        assert "every month" not in json.dumps(claim.extra)

    def test_a_formatted_run_keeps_its_annotations_and_link(self) -> None:
        block = _paragraph(self.notion, _run("See "), _run("the rate sheet", bold=True, url="https://x.test/r"))

        result = self.replace("rate sheet", "pricing sheet")

        assert result.exit_code == 0, result.output
        edited = _runs(self.notion, block)[1]
        assert edited["plain_text"] == "the pricing sheet"
        assert edited["annotations"]["bold"] is True
        assert edited["text"]["link"] == {"url": "https://x.test/r"}

    def test_adjacent_runs_with_one_formatting_are_merged_rather_than_refused(self) -> None:
        block = _paragraph(self.notion, _run("the rate "), _run("sheet"), _run(" tail", bold=True))

        result = self.replace("rate sheet", "price list")

        assert result.exit_code == 0, result.output
        assert [run["plain_text"] for run in _runs(self.notion, block)] == ["the price list", " tail"]

    def test_a_table_cell_is_replaced_in_place(self) -> None:
        row = {"type": "table_row", "table_row": {"cells": [[_run("LTV")], [_run("80 %")]]}}
        table = self.notion.add({"type": "table", "table": {"table_width": 2, "children": [row]}})
        row_id = self.notion.children[table][0]

        result = self.replace("80 %", "75 %")

        assert result.exit_code == 0, result.output
        assert self.notion.blocks[row_id]["table_row"]["cells"][1][0]["plain_text"] == "75 %"


class TestRefusals(ReplaceCase):
    def test_no_occurrence_is_refused_and_nothing_is_written(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every year", "every month")

        assert result.exit_code == 19, result.output
        assert "0 times" in result.output
        assert _patches(self.notion) == []

    def test_two_occurrences_are_refused_naming_both_blocks(self) -> None:
        first = _paragraph(self.notion, _run("fixed rate"))
        second = _paragraph(self.notion, _run("a fixed rate again"))

        result = self.replace("fixed rate", "variable rate")

        assert result.exit_code == 19, result.output
        assert "2 times" in result.output
        assert first in result.output
        assert second in result.output
        assert _patches(self.notion) == []

    def test_text_spanning_two_blocks_is_refused_as_such(self) -> None:
        _paragraph(self.notion, _run("first line"))
        _paragraph(self.notion, _run("second line"))

        result = self.replace("line\nsecond", "x")

        assert result.exit_code == 19, result.output
        assert "spans several blocks" in result.output

    def test_differently_formatted_runs_are_refused_rather_than_flattened(self) -> None:
        block = _paragraph(self.notion, _run("the "), _run("rate", bold=True), _run(" sheet"))

        result = self.replace("rate sheet", "price list")

        assert result.exit_code == 19, result.output
        assert "formatting" in result.output
        assert _runs(self.notion, block)[1]["annotations"]["bold"] is True
        assert _patches(self.notion) == []

    def test_a_page_outside_every_allowed_root_is_refused_even_on_a_dry_run(self) -> None:
        self.assert_refused_by_the_write_scope(allowed=[], denied=[])

    def test_a_page_whose_chain_misses_the_allowed_root_configured_elsewhere_is_refused(self) -> None:
        self.assert_refused_by_the_write_scope(allowed=[_ELSEWHERE], denied=[])

    def test_a_page_under_a_denied_root_is_refused_even_on_a_dry_run(self) -> None:
        self.assert_refused_by_the_write_scope(allowed=[self.notion.page_id], denied=[self.notion.page_id])

    def assert_refused_by_the_write_scope(self, *, allowed: list[str], denied: list[str]) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: (allowed, denied))

        for extra in ((), ("--dry-run",)):
            result = self.replace("every quarter", "every month", *extra)

            assert result.exit_code == 17, result.output
            assert "never writes" in result.output
        assert _patches(self.notion) == []
        assert not OutboundClaim.objects.exists()

    def test_a_replace_the_re_read_does_not_confirm_exits_with_the_not_landed_code(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.suppress_block_updates = True

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 9, result.output
        assert "treat the write as failed" in result.output


class TestDryRun(ReplaceCase):
    def test_a_dry_run_prints_the_block_and_the_diff_and_writes_nothing(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every month", "--dry-run")

        assert result.exit_code == 0, result.output
        assert block in result.output
        assert "-Rates reset every quarter." in result.output
        assert "+Rates reset every month." in result.output
        assert _patches(self.notion) == []
        assert self.notion.text_of(block) == "Rates reset every quarter."
        assert not OutboundClaim.objects.exists()

    def test_a_dry_run_prints_one_json_document_on_stdout_and_the_human_diff_on_stderr(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every month", "--dry-run")

        payload = json.loads(result.stdout)
        assert payload["outcome"] == "dry_run"
        assert payload["block_id"] == block
        assert (payload["before"], payload["after"]) == ("Rates reset every quarter.", "Rates reset every month.")
        assert "+Rates reset every month." in payload["diff"]
        assert "dry run — nothing written" in result.stderr
        assert "+Rates reset every month." in result.stderr


class TestInlineText(ReplaceCase):
    def inline(self, *args: str) -> typer.testing.Result:
        return self.runner.invoke(notion_app, ["replace", self.notion.page_id, *args])

    def test_old_and_new_text_replace_with_no_file_on_disk(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.inline("--old-text", "every quarter", "--new-text", "every month")

        assert result.exit_code == 0, result.output
        assert self.notion.text_of(block) == "Rates reset every month."

    def test_an_empty_new_text_deletes_the_span(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.inline("--old-text", " every quarter", "--new-text", "")

        assert result.exit_code == 0, result.output
        assert self.notion.text_of(block) == "Rates reset."

    def test_inline_text_is_verbatim_so_a_trailing_newline_is_part_of_the_anchor(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.inline("--old-text", "every quarter\n", "--new-text", "every month")

        assert result.exit_code == 19, result.output

    def test_each_anchor_takes_exactly_one_of_file_or_text_and_notion_is_not_asked(self) -> None:
        (self.tmp_path / "old.txt").write_text("a\n", encoding="utf-8")
        (self.tmp_path / "new.txt").write_text("b\n", encoding="utf-8")
        old_file, new_file = str(self.tmp_path / "old.txt"), str(self.tmp_path / "new.txt")
        cases = [
            ("--old-text", "a", "--old-file", old_file, "--new-text", "b"),
            ("--old-text", "a", "--new-text", "b", "--new-file", new_file),
            ("--new-text", "b"),
            ("--old-text", "a"),
        ]

        for args in cases:
            with self.subTest(args=args):
                result = self.inline(*args)

                assert result.exit_code == 1, result.output
                assert "exactly one of" in result.stderr
        assert self.notion.requests == []


class TestTheWriteMatchesWhatWasPlanned(ReplaceCase):
    def test_a_block_edited_after_the_walk_is_refused_and_not_overwritten(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.edit_on_read[block] = "Rates reset every quarter, per the bank's mail."

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 20, result.output
        assert "block changed" in result.output
        assert _patches(self.notion) == []
        assert self.notion.text_of(block) == "Rates reset every quarter, per the bank's mail."

    def test_a_write_that_lost_its_formatting_is_not_reported_as_landed(self) -> None:
        _paragraph(self.notion, _run("See "), _run("the rate sheet", bold=True))
        self.notion.drop_formatting_on_update = True

        result = self.replace("rate sheet", "pricing sheet")

        assert result.exit_code == 9, result.output

    def test_replacing_text_with_itself_is_refused_before_notion_is_asked(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every quarter")

        assert result.exit_code == 1, result.output
        assert self.notion.requests == []

    def test_text_inside_a_synced_block_is_named_as_unsearched(self) -> None:
        synced = self.notion.add(
            {"type": "synced_block", "synced_block": {"synced_from": None, "children": [_para_payload("hidden rule")]}}
        )

        result = self.replace("hidden rule", "visible rule")

        assert result.exit_code == 19, result.output
        assert synced in result.output
        assert "not searched" in result.output
        assert "check the exact characters" not in result.output

    def test_a_dry_run_sends_nothing_but_reads(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every month", "--dry-run")

        assert result.exit_code == 0, result.output
        assert {method for method, _path in self.notion.requests} == {"GET"}


class TestEveryWriteIsInTheLedger(ReplaceCase):
    def test_a_write_the_re_read_does_not_confirm_is_recorded_as_drift(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.suppress_block_updates = True

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 9, result.output
        claim = OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT)
        assert claim.verified_at is None
        assert claim.drift_detected is True

    def test_an_edit_that_returns_to_an_earlier_state_gets_its_own_row(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))

        for old, new in (("quarter", "month"), ("month", "quarter"), ("quarter", "month")):
            assert self.replace(old, new).exit_code == 0

        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.NOTION_EDIT, verified_at__isnull=False).count() == 3


class TestARetryAndAPartialWriteAreHonest(ReplaceCase):
    def test_an_edit_to_another_cell_of_the_row_is_refused_and_not_overwritten(self) -> None:
        row = {"type": "table_row", "table_row": {"cells": [[_run("LTV")], [_run("80 %")]]}}
        table = self.notion.add({"type": "table", "table": {"table_width": 2, "children": [row]}})
        row_id = self.notion.children[table][0]
        self.notion.edit_cell_on_read[row_id] = (0, "LTV max")

        result = self.replace("80 %", "75 %")

        assert result.exit_code == 20, result.output
        assert _patches(self.notion) == []
        assert self.notion.blocks[row_id]["table_row"]["cells"][0][0]["plain_text"] == "LTV max"

    def test_a_retry_of_a_replace_whose_new_text_holds_the_old_is_already_applied(self) -> None:
        block = _paragraph(self.notion, _run("Rates reset every quarter."))
        new = "every quarter, per the bank's mail"
        assert self.replace("every quarter", new).exit_code == 0

        result = self.replace("every quarter", new)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "already applied"
        assert self.notion.text_of(block) == "Rates reset every quarter, per the bank's mail."
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.NOTION_EDIT).count() == 1

    def test_a_drift_row_carries_no_page_text(self) -> None:
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.suppress_block_updates = True

        assert self.replace("every quarter", "every month").exit_code == 9

        claim = OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT)
        stored = claim.drift_reason + json.dumps(claim.extra)
        for fragment in ("Rates reset", "every quarter", "every month"):
            assert fragment not in stored

    def test_a_write_whose_read_back_failed_after_the_patch_leaves_an_unverified_row(self) -> None:
        self.monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.fail_reads_after_update = (503, "service_unavailable")

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 22, result.output
        claim = OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT)
        assert claim.extra["outcome"] == "unverified"
        assert "Rates reset" not in claim.drift_reason + json.dumps(claim.extra)

    def test_a_patch_whose_response_failed_after_it_applied_leaves_an_unverified_row(self) -> None:
        self.monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.update_then_fail = (502, "bad_gateway")

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 22, result.output
        assert OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT).extra["outcome"] == "unverified"

    def test_a_patch_that_timed_out_after_it_was_sent_leaves_an_unverified_row(self) -> None:
        self.monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        _paragraph(self.notion, _run("Rates reset every quarter."))
        self.notion.update_raises = (httpx.ReadTimeout, True)

        result = self.replace("every quarter", "every month")

        assert result.exit_code == 22, result.output
        assert OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT).extra["outcome"] == "unverified"


class TestAWriteNotionRefusedKeepsItsOwnCodeAndNoRow(ReplaceCase):
    def assert_refused_with(self, exit_code: int) -> None:
        self.monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        block = _paragraph(self.notion, _run("Rates reset every quarter."))

        result = self.replace("every quarter", "every month")

        assert result.exit_code == exit_code, result.output
        assert "state is unknown" not in result.output
        assert not OutboundClaim.objects.exists()
        assert self.notion.text_of(block) == "Rates reset every quarter."

    def test_a_rate_limited_patch_exits_8(self) -> None:
        self.notion.refuse_update = (429, "rate_limited")
        self.assert_refused_with(8)

    def test_a_patch_notion_rejects_as_invalid_exits_7(self) -> None:
        self.notion.refuse_update = (400, "validation_error")
        self.assert_refused_with(7)

    def test_a_patch_on_a_block_no_longer_shared_exits_6(self) -> None:
        self.notion.refuse_update = (404, "object_not_found")
        self.assert_refused_with(6)

    def test_a_connection_that_never_opened_exits_1(self) -> None:
        self.notion.update_raises = (httpx.ConnectError, False)
        self.assert_refused_with(1)

    def test_a_guard_refusal_at_write_time_exits_17(self) -> None:
        scopes = iter([([self.notion.page_id], [])])
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: next(scopes, ([self.notion.page_id], [self.notion.page_id])))
        self.assert_refused_with(17)
