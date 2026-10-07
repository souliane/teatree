from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates.privacy_gate import PrivacyGateResult, PrivacyMatch
from teatree.core.issue_hygiene import DescriptionRemoval, remove_description_section
from teatree.core.issue_writes.section_removal import DescriptionRemovalConflictError
from teatree.core.models import ConsolidatedMemory, Rubric, Task, Ticket, TicketSweepRun
from teatree.core.models.dream_gap_ledger import pending_entries
from teatree.core.self_forge_identities import NOT_SELF_AUTHORED_REASON
from teatree.hooks import _repo_visibility
from teatree.loops.dream.gap_attach import DISPOSITION_CRITERION, GapAttachError, attach_dream_gaps
from tests.factories import record_test_plan
from tests.teatree_loops.dream._gap_attach_support import (
    HOST_URL,
    UMBRELLA,
    _current_fold,
    _forge,
    _host,
    _section_marker,
    _umbrella,
)


@pytest.fixture(autouse=True)
def _private_host_by_default(monkeypatch: pytest.MonkeyPatch, _configured_dream_publication: None) -> None:
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")


class TestAttachProvedByTheForge(TestCase):
    @staticmethod
    def _set_sensitive_memory() -> None:
        ConsolidatedMemory.objects.filter(cluster_key="g1").update(
            rule="Read /home/example/private/rules.md before proceeding.",
            verified_citation="internal.example.com recorded the failure",
        )

    @staticmethod
    def _visibility(*, verdict: bool | None):
        return patch.object(
            _repo_visibility,
            "probe_visibility",
            return_value="PRIVATE" if verdict is True else "PUBLIC" if verdict is False else None,
        )

    def _assert_public_fold(self, *, visibility: bool | None) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        self._set_sensitive_memory()
        checks = self._visibility(verdict=visibility)
        with checks:
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        host.refresh_from_db()
        body = forge.get_issue(HOST_URL)["description"]
        assert outcome.attached == ["g1"]
        assert "dream-gap g1 — Fix g1" in body
        assert "/home/example/private/rules.md" not in body
        assert "internal.example.com" not in body
        assert "/home/example/private/rules.md" in host.context
        assert "internal.example.com" in host.context

    def test_public_host_keeps_full_gap_only_in_ticket_context(self) -> None:
        self._assert_public_fold(visibility=False)

    def test_unconfirmed_host_keeps_full_gap_only_in_ticket_context(self) -> None:
        self._assert_public_fold(visibility=None)

    def test_private_host_keeps_the_verbatim_rule_and_citation_in_the_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        self._set_sensitive_memory()
        checks = self._visibility(verdict=True)
        with checks:
            attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert "/home/example/private/rules.md" in body
        assert "Cited: internal.example.com recorded the failure" in body

    def test_the_public_umbrella_gets_only_the_short_title(self) -> None:
        umbrella, forge = _umbrella("g1"), _forge()
        self._set_sensitive_memory()
        checks = self._visibility(verdict=False)
        with checks, patch.object(Ticket, "schedule_implementing"):
            attach_dream_gaps(umbrella, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(UMBRELLA)["description"]
        assert "dream-gap g1 — Fix g1" in body
        assert "/home/example/private/rules.md" not in body
        assert "internal.example.com" not in body

    def test_public_short_title_must_pass_the_forge_publication_scan(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        checks = self._visibility(verdict=False)
        blocked = PrivacyGateResult(
            target_repo="o/factory",
            is_public=True,
            matches=(PrivacyMatch(pattern_name="redact:private", matched_text="Fix", position=0),),
        )
        with (
            checks,
            patch("teatree.loops.dream.gap_attach.scan_outbound_text", return_value=blocked) as scan,
        ):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == []
        assert "failed the publication scan" in outcome.refusal
        scan.assert_called_once_with(text="Fix g1", target_repo="o/factory", forge="gitlab")
        forge.update_issue.assert_not_called()

    def test_the_fold_lands_on_the_host_issue_and_the_gaps_move_to_the_host(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()

        outcome = attach_dream_gaps(
            host, [{"gap_key": "g1", "theme": "gates"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=forge
        )

        host.refresh_from_db()
        umbrella.refresh_from_db()
        body = forge.get_issue(HOST_URL)["description"]
        assert "## Folded in: dream-gap g1 — Fix g1" in body
        assert "Run the lane before pushing (g2)." in body
        assert outcome.attached == ["g1", "g2"]
        assert [(e["gap_key"], e["theme"]) for e in host.extra["dream_gap_batch"]] == [("g1", "gates"), ("g2", "")]
        assert pending_entries(umbrella) == []

    def test_the_host_gains_the_disposition_criterion_once(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()

        attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        attach_dream_gaps(host, [{"gap_key": "g2"}], umbrella=umbrella, code_host=forge)

        texts = list(Rubric.objects.get(ticket=host).criteria.values_list("text", flat=True))
        assert texts == [DISPOSITION_CRITERION.format(pk=host.pk)]

    def test_an_unplanned_early_host_is_routed_to_planning_around_the_gaps(self) -> None:
        umbrella, host = _umbrella("g1"), _host()

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge())

        assert outcome.task is not None
        assert outcome.task.phase == "planning"
        assert "[g1] Fix g1" in outcome.task.execution_reason

    def test_a_planned_host_keeps_its_work_and_gets_no_new_task(self) -> None:
        umbrella, host = _umbrella("g1"), _host(Ticket.State.PLAN_RECORDED)
        record_test_plan(host)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge())

        assert outcome.task is None
        assert outcome.attached == ["g1"]
        assert not Task.objects.filter(ticket=host).exists()
        assert Rubric.objects.get(ticket=host).criteria.count() == 1

    def test_the_umbrella_itself_is_the_fallback_host(self) -> None:
        umbrella = _umbrella("g1")
        forge = _forge()
        with patch.object(Ticket, "schedule_implementing") as schedule:
            outcome = attach_dream_gaps(umbrella, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.attached == ["g1"]
        assert pending_entries(umbrella) == []
        assert [e["gap_key"] for e in umbrella.extra["dream_gap_batch"]] == ["g1"]
        schedule.assert_called_once()


class TestAttachRefusesWithoutProof(TestCase):
    def test_a_fold_the_forge_did_not_keep_records_nothing(self) -> None:
        umbrella, host = _umbrella("g1"), _host()

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge(persists=False))

        host.refresh_from_db()
        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert "could not confirm the description append" in outcome.refusal
        assert "dream_gap_batch" not in host.extra
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]
        assert not Rubric.objects.filter(ticket=host).exists()
        assert not Task.objects.exists()

    def test_a_fold_the_forge_summarised_records_nothing(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        keep_markers_only = forge.update_issue.side_effect

        def _summarise(**kwargs: object) -> dict[str, int]:
            kept = [line for line in str(kwargs["body"]).splitlines() if line.startswith("## ")]
            return keep_markers_only(body="\n".join(kept))

        forge.update_issue.side_effect = _summarise

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert "could not confirm the description append" in outcome.refusal
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]

    def test_an_unreadable_host_issue_records_nothing(self) -> None:
        umbrella, host = _umbrella("g1"), _host()
        forge = _forge()
        forge.get_issue.side_effect = RuntimeError("forge down")

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.refusal == (
            f"the fold into {HOST_URL} did not land: refusing to modify {HOST_URL}: "
            "authored by (unknown) — could not read the issue (forge down)"
        )
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]

    def test_a_host_someone_else_filed_is_refused_by_that_cause(self) -> None:
        umbrella, host = _umbrella("g1"), _host()
        forge = _forge(author="a.colleague")

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.refusal == (
            f"the fold into {HOST_URL} did not land: refusing to modify {HOST_URL}: "
            f"authored by a.colleague — {NOT_SELF_AUTHORED_REASON}"
        )
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]
        forge.update_issue.assert_not_called()

    @staticmethod
    def _assert_refused_before_any_write(state: str) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(state), _forge()

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        host.refresh_from_db()
        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert outcome.refusal == f"host {HOST_URL} is {state}: no new gaps"
        assert "dream_gap_batch" not in (host.extra or {})
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]
        forge.update_issue.assert_not_called()

    def test_a_merged_host_is_refused_before_any_write(self) -> None:
        self._assert_refused_before_any_write(Ticket.State.MERGED)

    def test_an_ignored_host_is_refused_before_any_write(self) -> None:
        self._assert_refused_before_any_write(Ticket.State.IGNORED)

    def test_a_stale_host_instance_cannot_write_to_a_terminal_host(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == []
        assert outcome.refusal == f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps"
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]
        forge.update_issue.assert_not_called()

    def test_a_host_closed_during_the_forge_write_keeps_the_gap_pending(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect

        def close_after_write(**kwargs: object) -> dict[str, int]:
            result = update(**kwargs)
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = close_after_write

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert outcome.refusal == f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps"
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]
        assert forge.get_issue(HOST_URL)["description"] == "## Gates that cost a day\n"

    def test_a_failed_compensation_reports_that_a_fold_may_remain_on_a_terminal_host(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def fail_second_write(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            if writes == 2:
                msg = "forge rollback unavailable"
                raise RuntimeError(msg)
            result = update(**kwargs)
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = fail_second_write

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            "compensation failed (forge rollback unavailable): fold may remain on a terminal host"
        )
        assert "## Folded in: dream-gap g1" in forge.get_issue(HOST_URL)["description"]
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]

    def test_compensation_refuses_a_write_that_loses_unrelated_text(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def truncate_second_write(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            else:
                body = body.replace("Gates that cost a day", "Gates")
            return update(body=body)

        forge.update_issue.side_effect = truncate_second_write

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == []
        marker = _section_marker(str(forge.update_issue.call_args_list[0].kwargs["body"]))
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            f"compensation failed (could not confirm complete removal of {marker} from {HOST_URL}): "
            "fold may remain on a terminal host"
        )
        assert forge.get_issue(HOST_URL)["description"] == "## Gates\n"
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]

        marker = "<!-- t3-hygiene:0000000000000000 -->"
        body = f"Human text\n\n{marker}\n\nOwned fold"
        cases = (
            ("different body", "Human text", False, True, "description changed before removal of"),
            (
                body,
                f"Edited human text\n\n{marker}\n\nOwned fold",
                True,
                False,
                "could not confirm description edit of",
            ),
            (body, "Human text", False, False, "could not confirm complete removal of"),
        )
        for expected, replacement, retain, persists, reason in cases:
            case_forge = _forge(body=body, persists=persists)
            removal = DescriptionRemoval(
                marker=marker,
                expected_body=expected,
                replacement_body=replacement,
                action="dream_gap_compensate",
                retain_marker=retain,
            )
            with self.subTest(reason=reason):
                with pytest.raises(DescriptionRemovalConflictError) as error:
                    remove_description_section(host=case_forge, issue_url=HOST_URL, removal=removal)
                suffix = f" on {HOST_URL}" if reason != "could not confirm complete removal of" else f" from {HOST_URL}"
                assert str(error.value) == f"{reason} {marker}{suffix}"

    def test_compensation_preserves_a_later_issue_section(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def add_later_section(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                body += "\n\n## Later note\n\nKeep this."
                Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return update(body=body)

        forge.update_issue.side_effect = add_later_section

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert outcome.attached == []
        assert "## Folded in: dream-gap g1" not in body
        assert "## Later note\n\nKeep this." in body
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]

    def test_a_nonprefix_partial_fold_on_a_newly_terminal_host_is_refused(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()
        update = forge.update_issue.side_effect

        def drop_second_marker_and_close(**kwargs: object) -> dict[str, int]:
            result = update(body=str(kwargs["body"]).replace("## Folded in: dream-gap g2", "## [redacted]"))
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = drop_second_marker_and_close

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == []
        marker = _section_marker(str(forge.update_issue.call_args_list[0].kwargs["body"]))
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            f"the host issue {HOST_URL} lost the fold of: dream-gap g2; "
            f"compensation failed (fold section {marker} differs from the rendered body): "
            "fold may remain on a terminal host"
        )
        assert "## [redacted]" in forge.get_issue(HOST_URL)["description"]
        assert sorted(entry["gap_key"] for entry in pending_entries(umbrella)) == ["g1", "g2"]

    def test_compensation_preserves_a_human_quote_before_the_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect

        def quote_then_close(**kwargs: object) -> dict[str, int]:
            body = str(kwargs["body"])
            marker = next(line for line in body.splitlines() if line.startswith("<!-- t3-hygiene:"))
            result = update(body=f"Human quote:\n{marker}\nKeep this.\n\n{body}")
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = quote_then_close
        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert outcome.attached == []
        assert "Human quote:" in body
        assert "Keep this." in body
        marker = _section_marker(body)
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            f"compensation failed (fold section {marker} has no unique start marker): "
            "fold may remain on a terminal host"
        )
        assert "## Folded in: dream-gap g1" in body
        forge.update_issue.assert_called_once()

    def test_compensation_preserves_a_human_quote_after_the_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect

        def quote_after_fold(**kwargs: object) -> dict[str, int]:
            body = str(kwargs["body"])
            marker = next(line for line in body.splitlines() if line.startswith("<!-- t3-hygiene:"))
            result = update(body=f"{body}\n\n## Human note\n\n{marker}\nKeep this.")
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = quote_after_fold
        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert outcome.attached == []
        marker = _section_marker(body)
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            f"compensation failed (fold section {marker} has no unique start marker): "
            "fold may remain on a terminal host"
        )
        assert "## Folded in: dream-gap g1" in body
        assert "## Human note" in body
        assert "Keep this." in body
        forge.update_issue.assert_called_once()

    @staticmethod
    def _assert_bad_markers_are_refused(*, duplicate: bool) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect

        def corrupt_then_close(**kwargs: object) -> dict[str, int]:
            body = str(kwargs["body"])
            marker = next(line for line in body.splitlines() if line.startswith("<!-- t3-hygiene:"))
            if duplicate:
                body = f"{body}\n\n{body[body.index(marker) :]}"
            else:
                body = body.replace(marker, f"{marker}\n<!-- t3-hygiene:0000000000000000 -->", 1)
            result = update(body=body)
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = corrupt_then_close
        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        marker = _section_marker(str(forge.update_issue.call_args.kwargs["body"]))
        assert outcome.refusal == (
            f"host {HOST_URL} is {Ticket.State.MERGED}: no new gaps; "
            f"compensation failed (fold section {marker} "
            f"{'has no unique start marker' if duplicate else 'contains a nested start marker'}): "
            "fold may remain on a terminal host"
        )
        assert "## Folded in:" in forge.get_issue(HOST_URL)["description"]
        forge.update_issue.assert_called_once()

    def test_compensation_refuses_nested_fold_markers(self) -> None:
        self._assert_bad_markers_are_refused(duplicate=False)

    def test_compensation_refuses_duplicate_fold_markers(self) -> None:
        self._assert_bad_markers_are_refused(duplicate=True)

    def test_a_gap_not_pending_is_refused_before_any_write(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()

        with pytest.raises(GapAttachError) as error:
            attach_dream_gaps(host, [{"gap_key": "g1"}, {"gap_key": "zz"}], umbrella=umbrella, code_host=forge)

        assert str(error.value) == f"not pending on {UMBRELLA}: zz"
        forge.update_issue.assert_not_called()


class TestAttachBookkeepingIsAtomic(TestCase):
    def test_a_failure_after_the_fold_leaves_the_gaps_pending_and_the_host_unrecorded(self) -> None:
        umbrella, host = _umbrella("g1"), _host()

        with patch.object(Rubric, "add_criteria", side_effect=RuntimeError("db down")), pytest.raises(RuntimeError):
            attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge())

        host.refresh_from_db()
        umbrella.refresh_from_db()
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]
        assert "dream_gap_batch" not in (host.extra or {})


class TestAttachChecksTheTextItActuallyWrote(TestCase):
    @staticmethod
    def _redacting(text: str, **_: object) -> str:
        return text.replace("went red", "[redacted]")

    def test_attach_preserves_every_byte_of_flagged_user_text(self) -> None:
        original = "## Human note\n\nSENSITIVE-TOKEN\n  trailing  \n"
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=original)

        with patch(
            "teatree.core.issue_hygiene.route_forge_write",
            side_effect=lambda text, **_: text.replace("SENSITIVE-TOKEN", "[redacted]").replace(
                "went red", "[redacted]"
            ),
        ):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert forge.get_issue(HOST_URL)["description"].startswith(original + "\n\n")
        assert "Cited: task for g1 [redacted]" in forge.get_issue(HOST_URL)["description"]

    def test_repair_preserves_every_byte_of_flagged_user_text(self) -> None:
        original = "## Human note\n\nSENSITIVE-TOKEN\n  trailing  \n"
        folded = _current_fold("g1")
        section = folded[folded.index("<!-- t3-hygiene:") :]
        before, rest = section.split("Run the lane before pushing (g1).", maxsplit=1)
        end = next(line for line in rest.splitlines() if line.startswith("<!-- t3-dream-gap-fold-end:"))
        incomplete = f"{original}\n\n{before.rstrip()}\n\n{end}"
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=incomplete)

        with patch(
            "teatree.core.issue_hygiene.route_forge_write",
            side_effect=lambda text, **_: text.replace("SENSITIVE-TOKEN", "[redacted]").replace(
                "went red", "[redacted]"
            ),
        ):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert forge.get_issue(HOST_URL)["description"].startswith(original + "\n\n")
        assert "Cited: task for g1 [redacted]" in forge.get_issue(HOST_URL)["description"]

    def test_compensation_preserves_every_byte_of_flagged_user_text(self) -> None:
        original = "## Human note\n\nSENSITIVE-TOKEN\n  trailing  \n"
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=original)
        update = forge.update_issue.side_effect

        def close_after_write(**kwargs: object) -> dict[str, int]:
            result = update(**kwargs)
            Ticket.objects.filter(pk=host.pk).update(state=Ticket.State.MERGED)
            return result

        forge.update_issue.side_effect = close_after_write
        with patch(
            "teatree.core.issue_hygiene.route_forge_write",
            side_effect=lambda text, **_: text.replace("SENSITIVE-TOKEN", "[redacted]"),
        ):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == []
        assert forge.get_issue(HOST_URL)["description"] == original

    def test_a_scrubbed_fold_is_proved_against_the_scrubbed_text(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()

        with patch("teatree.core.issue_hygiene.route_forge_write", side_effect=self._redacting):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert (outcome.refusal, outcome.attached) == ("", ["g1"])
        assert "[redacted]" in forge.get_issue(HOST_URL)["description"]

    def test_a_wholly_redacted_body_leaves_the_gap_pending(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()

        def _erase_body(text: str, **_: object) -> str:
            before, marker, body = text.partition("## Folded in: dream-gap g1")
            heading = marker + body.split("\n", maxsplit=1)[0]
            return before + heading + "\n[redacted]\n"

        with patch("teatree.core.issue_hygiene.route_forge_write", side_effect=_erase_body):
            outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert outcome.refusal == f"the host issue {HOST_URL} lost the fold of: dream-gap g1"
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == ["g1"]

    def test_a_confirmed_append_whose_scrub_dropped_a_gaps_fold_marker_records_nothing(self) -> None:
        umbrella, host = _umbrella("g1", "g2"), _host()

        def _redact_g2(text: str, **_: object) -> str:
            return text.replace("## Folded in: dream-gap g2", "## [redacted]")

        with patch("teatree.core.issue_hygiene.route_forge_write", side_effect=_redact_g2):
            outcome = attach_dream_gaps(
                host, [{"gap_key": "g1"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=_forge()
            )

        umbrella.refresh_from_db()
        assert (outcome.attached, outcome.refusal) == ([], f"the host issue {HOST_URL} lost the fold of: dream-gap g2")
        assert sorted(e["gap_key"] for e in pending_entries(umbrella)) == ["g1", "g2"]

    def test_a_retry_over_an_already_scrubbed_fold_attaches_without_rewriting(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        with (
            patch("teatree.core.issue_hygiene.route_forge_write", side_effect=self._redacting),
            patch.object(Rubric, "add_criteria", side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError),
        ):
            attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        forge.update_issue.reset_mock()

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert (outcome.refusal, outcome.attached) == ("", ["g1"])
        forge.update_issue.assert_not_called()


class TestAttachRouting(TestCase):
    def test_an_unplanned_host_past_the_early_states_is_attached_without_a_task(self) -> None:
        umbrella, host = _umbrella("g1"), _host(Ticket.State.CODED)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge())

        assert (outcome.attached, outcome.task) == (["g1"], None)
        assert Rubric.objects.get(ticket=host).criteria.count() == 1


class TestAttachIsASweepMutation(TestCase):
    def test_the_host_is_counted_against_the_sweep_run_that_attached_to_it(self) -> None:
        umbrella, host = _umbrella("g1"), _host()
        run = TicketSweepRun.objects.begin(source="loop")

        attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=_forge(), sweep_run_id=run.run_id)

        run.refresh_from_db()
        assert run.changed_urls == [HOST_URL]

    def test_a_second_attach_folds_only_the_new_gap(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()
        attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        attach_dream_gaps(host, [{"gap_key": "g2"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert (body.count("## Folded in: dream-gap g1"), body.count("## Folded in: dream-gap g2")) == (1, 1)
