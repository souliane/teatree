"""The single top-down intake decision function (#3634)."""

from django.test import TestCase

from teatree.core.intake.factory_admission import (
    IntakeFacts,
    IntakeVerdict,
    decide_intake,
    decide_issue_intake,
    payload_labels,
    resolve_admit_label,
)
from teatree.core.models import ConfigSetting


def _facts(
    *,
    labels: frozenset[str] = frozenset(),
    work_exists: bool = False,
    author_trusted: bool = False,
) -> IntakeFacts:
    return IntakeFacts(labels=labels, work_exists=work_exists, author_trusted=author_trusted)


class TestDecisionTableOrder:
    """Rules 1-5 of the issue's table, evaluated top-down, first match wins."""

    def test_needs_triage_ignores_even_a_trusted_author(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"needs-triage", "t3-auto"}), author_trusted=True),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.IGNORE_NEEDS_TRIAGE
        assert not verdict.acts

    def test_existing_work_ignores_even_a_trusted_author(self) -> None:
        verdict = decide_intake(_facts(work_exists=True, author_trusted=True), admit_label="t3-auto")
        assert verdict is IntakeVerdict.IGNORE_WORK_EXISTS
        assert not verdict.acts

    def test_needs_triage_outranks_existing_work(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"needs-triage"}), work_exists=True),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.IGNORE_NEEDS_TRIAGE

    def test_trusted_author_acts_with_no_label_and_no_assignment(self) -> None:
        verdict = decide_intake(_facts(author_trusted=True), admit_label="t3-auto")
        assert verdict is IntakeVerdict.ACT_TRUSTED_AUTHOR
        assert verdict.acts

    def test_untrusted_author_with_admit_label_acts(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"t3-auto"})),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.ACT_ADMITTED
        assert verdict.acts

    def test_untrusted_author_without_label_is_ignored_fail_closed(self) -> None:
        verdict = decide_intake(_facts(labels=frozenset({"bug"})), admit_label="t3-auto")
        assert verdict is IntakeVerdict.IGNORE_NOT_ADMITTED
        assert not verdict.acts

    def test_empty_admit_label_never_admits_an_untrusted_author(self) -> None:
        """An unset admit label must not degrade into "every label admits"."""
        verdict = decide_intake(_facts(labels=frozenset({"", "bug"})), admit_label="")
        assert verdict is IntakeVerdict.IGNORE_NOT_ADMITTED


class TestPayloadLabels:
    def test_reads_both_forge_label_shapes(self) -> None:
        assert payload_labels({"labels": ["a", {"name": "b"}]}) == frozenset({"a", "b"})

    def test_a_non_list_labels_field_is_empty(self) -> None:
        assert payload_labels({"labels": "t3-auto"}) == frozenset()

    def test_a_missing_labels_field_is_empty(self) -> None:
        assert payload_labels({}) == frozenset()


class TestPayloadFacade:
    """``decide_issue_intake`` extracts the facts from a raw forge payload."""

    def test_reads_github_and_gitlab_label_shapes(self) -> None:
        github = {"labels": [{"name": "t3-auto"}]}
        gitlab = {"labels": ["t3-auto"]}
        for payload in (github, gitlab):
            assert (
                decide_issue_intake(payload, author_trusted=False, work_exists=False, admit_label="t3-auto")
                is IntakeVerdict.ACT_ADMITTED
            )

    def test_trusted_author_flag_is_supplied_by_the_caller(self) -> None:
        assert (
            decide_issue_intake({}, author_trusted=True, work_exists=False, admit_label="t3-auto")
            is IntakeVerdict.ACT_TRUSTED_AUTHOR
        )


class TestResolveAdmitLabel(TestCase):
    def test_defaults_to_the_shipped_t3_auto_label(self) -> None:
        assert resolve_admit_label("") == "t3-auto"

    def test_reads_the_issue_implementer_label_setting(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "admit-me")
        assert resolve_admit_label("") == "admit-me"
