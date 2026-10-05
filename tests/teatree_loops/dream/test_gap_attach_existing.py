from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.issue_hygiene import IssueWriteConflictError
from teatree.core.models import ConsolidatedMemory, Rubric
from teatree.core.models.dream_gap_ledger import pending_entries
from teatree.hooks import _repo_visibility
from teatree.loops.dream.gap_attach import _FoldResult, _validated_fold, attach_dream_gaps
from tests.teatree_loops.dream._gap_attach_support import HOST_URL, _forge, _host, _section_marker, _umbrella


@pytest.fixture(autouse=True)
def _private_host_by_default(monkeypatch: pytest.MonkeyPatch, _configured_dream_publication: None) -> None:
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")


class TestAPreExistingFoldMustCarryTheGap(TestCase):
    HEADING_ONLY = "## Gates that cost a day\n\n## Folded in: dream-gap g1 — Fix g1\n"

    def test_a_later_ordinary_section_cannot_supply_a_heading_only_fold(self) -> None:
        member_body = "Dream gap `g1` — citation: cited.\n\nRun the lane before pushing (g1)."
        body = "## Folded in: dream-gap g1 — Fix g1\n\n## Other notes\n\n" + member_body
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=body)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert forge.get_issue(HOST_URL)["description"].count("## Folded in: dream-gap g1") == 2

    def test_a_later_setext_section_cannot_supply_a_heading_only_fold(self) -> None:
        body = "## Folded in: dream-gap g1 — Fix g1\n\nOther notes\n---\n\nRun the lane before pushing (g1)."
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=body)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert forge.get_issue(HOST_URL)["description"].count("## Folded in: dream-gap g1") == 2

    def test_a_deeper_heading_inside_the_member_body_still_verifies(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        row = ConsolidatedMemory.objects.get(cluster_key="g1")
        row.rule = "### Detail\n\nRun the lane before pushing (g1)."
        row.save(update_fields=["rule"])
        with patch.object(Rubric, "add_criteria", side_effect=RuntimeError("db down")), pytest.raises(RuntimeError):
            attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        forge.update_issue.reset_mock()

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        forge.update_issue.assert_not_called()

    def test_an_embedded_same_level_heading_is_demoted_when_folding(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        row = ConsolidatedMemory.objects.get(cluster_key="g1")
        row.rule = "## Rule heading\nRun the lane before pushing (g1)."
        row.save(update_fields=["rule"])

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert "### Rule heading" in forge.get_issue(HOST_URL)["description"]

    def test_an_embedded_setext_heading_is_demoted_when_folding(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        row = ConsolidatedMemory.objects.get(cluster_key="g1")
        row.rule = "Rule heading\n---\nRun the lane before pushing (g1)."
        row.save(update_fields=["rule"])

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert "### Rule heading" in forge.get_issue(HOST_URL)["description"]

    def test_a_heading_only_fold_is_refolded_with_the_gap_body(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=self.HEADING_ONLY)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert (outcome.refusal, outcome.attached) == ("", ["g1"])
        assert "Run the lane before pushing (g1)." in forge.get_issue(HOST_URL)["description"]

    def test_a_heading_only_fold_the_forge_will_not_fill_records_nothing(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge(body=self.HEADING_ONLY, persists=False)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        umbrella.refresh_from_db()
        assert outcome.attached == []
        assert outcome.refusal
        assert [e["gap_key"] for e in pending_entries(umbrella)] == ["g1"]

    def test_a_retry_repairs_an_incomplete_fold_with_its_digest_marker(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def truncate_first_write(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                before, fold_heading, rest = body.partition("## Folded in: dream-gap g1")
                body_text, end_marker, tail = rest.partition("<!-- t3-dream-gap-fold-end:")
                assert fold_heading
                assert end_marker
                body = before + fold_heading + body_text.split("\n", maxsplit=1)[0] + "\n\n" + end_marker + tail
            return update(body=body)

        forge.update_issue.side_effect = truncate_first_write

        first = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        incomplete = forge.get_issue(HOST_URL)["description"]
        assert first.attached == []
        assert first.refusal
        assert "<!-- t3-hygiene:" in incomplete
        assert "## Folded in: dream-gap g1" in incomplete
        assert "Run the lane before pushing (g1)." not in incomplete

        retried = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        repaired = forge.get_issue(HOST_URL)["description"]
        assert (retried.refusal, retried.attached) == ("", ["g1"])
        assert repaired.count("## Folded in: dream-gap g1") == 1
        assert "Run the lane before pushing (g1)." in repaired
        assert [entry["gap_key"] for entry in pending_entries(umbrella)] == []

    def test_reversed_manifest_repairs_a_truncated_fold_in_place(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def truncate_second_member(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                before, heading, rest = body.partition("## Folded in: dream-gap g1")
                assert heading
                end = next(line for line in rest.splitlines() if line.startswith("<!-- t3-dream-gap-fold-end:"))
                body = f"{before}{heading}{rest.splitlines()[0]}\n\n{end}"
            return update(body=body)

        forge.update_issue.side_effect = truncate_second_member
        first = attach_dream_gaps(host, [{"gap_key": "g2"}, {"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        assert first.attached == []
        forge.update_issue.reset_mock()

        retry = attach_dream_gaps(host, [{"gap_key": "g1"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert retry.attached == ["g1", "g2"]
        assert body.count("## Dream gaps folded in") == 1
        assert body.count("## Folded in: dream-gap g1") == 1
        assert body.count("## Folded in: dream-gap g2") == 1
        forge.update_issue.assert_called_once()

    def test_retry_replaces_a_truncated_two_gap_section_in_place(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g2"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def truncate_second_member(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                before, heading, rest = body.partition("## Folded in: dream-gap g2")
                assert heading
                end = next(line for line in rest.splitlines() if line.startswith("<!-- t3-dream-gap-fold-end:"))
                body = f"{before}{heading}{rest.splitlines()[0]}\n\n{end}"
            return update(body=body)

        forge.update_issue.side_effect = truncate_second_member
        first = attach_dream_gaps(host, [{"gap_key": "g1"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=forge)
        assert first.attached == []
        incomplete = forge.get_issue(HOST_URL)["description"]
        marker = next(line for line in incomplete.splitlines() if line.startswith("<!-- t3-hygiene:"))
        forge.update_issue.reset_mock()

        retry = attach_dream_gaps(host, [{"gap_key": "g1"}, {"gap_key": "g2"}], umbrella=umbrella, code_host=forge)

        body = forge.get_issue(HOST_URL)["description"]
        assert retry.attached == ["g1", "g2"]
        assert body.count(marker) == 1
        assert body.count("## Dream gaps folded in") == 1
        assert body.count("## Folded in: dream-gap g1") == 1
        assert body.count("## Folded in: dream-gap g2") == 1
        forge.update_issue.assert_called_once()

    def test_retry_refuses_a_human_edit_inside_an_incomplete_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def add_human_text(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                body = body.replace("Run the lane before pushing (g1).", "Human changed this rule.")
            return update(body=body)

        forge.update_issue.side_effect = add_human_text
        first = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        assert first.attached == []
        forge.update_issue.reset_mock()

        retry = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert "Human changed this rule." in forge.get_issue(HOST_URL)["description"]
        assert retry.attached == []
        marker = _section_marker(str(forge.get_issue(HOST_URL)["description"]))
        assert retry.refusal == (
            f"the fold into {HOST_URL} did not land: fold section {marker} differs from the rendered body"
        )
        forge.update_issue.assert_not_called()

    def test_retry_preserves_a_quoted_marker_before_an_incomplete_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1"), _host(), _forge()
        update = forge.update_issue.side_effect
        writes = 0

        def quote_and_truncate(**kwargs: object) -> dict[str, int]:
            nonlocal writes
            writes += 1
            body = str(kwargs["body"])
            if writes == 1:
                marker = next(line for line in body.splitlines() if line.startswith("<!-- t3-hygiene:"))
                body = body.replace("Run the lane before pushing (g1).", "")
                body = f"A human quoted this marker:\n{marker}\nKeep this.\n\n{body}"
            return update(body=body)

        forge.update_issue.side_effect = quote_and_truncate
        first = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)
        assert first.attached == []
        before_retry = forge.get_issue(HOST_URL)["description"]
        forge.update_issue.reset_mock()

        retry = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert retry.attached == []
        marker = _section_marker(before_retry)
        assert retry.refusal == (
            f"the fold into {HOST_URL} did not land: fold section {marker} has no unique start marker"
        )
        assert forge.get_issue(HOST_URL)["description"] == before_retry
        forge.update_issue.assert_not_called()

        marker = "<!-- t3-hygiene:0000000000000000 -->"
        end = "<!-- t3-dream-gap-fold-end:0000000000000000 -->"
        header = "\n\n## Dream gaps folded in (2026-10-01)\n\n"
        fold = _FoldResult(marker=marker, end_marker=end, section_body="body")
        bad_sections = (
            ("no marker", "has no unique start marker"),
            (marker + header + "body", "has no unique matching end marker"),
            (
                marker + header + "<!-- t3-hygiene:1111111111111111 -->\nbody\n\n" + end,
                "contains a nested start marker",
            ),
            (marker + "\n\n## Other heading (2026-10-01)\n\nbody\n\n" + end, "does not match the rendered heading"),
            (marker + header + "other\n\n" + end, "differs from the rendered body"),
        )
        for description, reason in bad_sections:
            with self.subTest(reason=reason), pytest.raises(IssueWriteConflictError) as error:
                _validated_fold(description, fold)
            assert str(error.value) == f"fold section {marker} {reason}"

    def test_a_fold_whose_key_extends_the_gaps_key_does_not_count_as_its_fold(self) -> None:
        umbrella, host, forge = _umbrella("g1", "g10"), _host(), _forge()
        attach_dream_gaps(host, [{"gap_key": "g10"}], umbrella=umbrella, code_host=forge)

        outcome = attach_dream_gaps(host, [{"gap_key": "g1"}], umbrella=umbrella, code_host=forge)

        assert outcome.attached == ["g1"]
        assert "Run the lane before pushing (g1)." in forge.get_issue(HOST_URL)["description"]
