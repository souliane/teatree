"""The single top-down intake decision function (#3634)."""

from typing import TYPE_CHECKING

from django.test import TestCase

from teatree.core.intake.factory_admission import (
    IntakeFacts,
    IntakeLabelPolicy,
    IntakeVerdict,
    decide_intake,
    decide_issue_intake,
    payload_body,
    payload_labels,
    resolve_admit_label,
    resolve_umbrella_labels,
)
from teatree.core.models import ConfigSetting

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

EPIC_BODY = "## Members\n\n- [x] #3848\n- [x] #3892\n- [x] #4035\n"

LEDGER_BODY = "## DO NOT CLOSE — standing ledger\n\n- [ ] **A gap** — with prose, so no child list.\n"


def _facts(
    *,
    labels: frozenset[str] = frozenset(),
    work_exists: bool = False,
    author_trusted: bool = False,
    umbrella_reason: str = "",
) -> IntakeFacts:
    return IntakeFacts(
        labels=labels,
        work_exists=work_exists,
        author_trusted=author_trusted,
        umbrella_reason=umbrella_reason,
    )


class TestDecisionTableOrder:
    """Every rule of the issue's table, evaluated top-down, first match wins."""

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

    def test_an_umbrella_row_ignores_even_a_trusted_author(self) -> None:
        """#4105: an epic is a poor intake UNIT — a bounded slot for an unbounded scope."""
        verdict = decide_intake(
            _facts(umbrella_reason="carries the 'epic' label", author_trusted=True),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.IGNORE_UMBRELLA
        assert not verdict.acts

    def test_an_umbrella_row_ignores_even_with_the_admit_label(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"t3-auto"}), umbrella_reason="lists 3 child issues"),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.IGNORE_UMBRELLA

    def test_needs_triage_outranks_umbrella(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"needs-triage"}), umbrella_reason="carries the 'epic' label"),
            admit_label="t3-auto",
        )
        assert verdict is IntakeVerdict.IGNORE_NEEDS_TRIAGE

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


class TestExcludeTier:
    """The overlay's ``exclude_labels`` is a hold like ``needs-triage`` (#4134)."""

    def test_an_excluded_label_ignores_even_a_trusted_author(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"interactive-implementation"}), author_trusted=True),
            admit_label="t3-auto",
            exclude_labels=frozenset({"interactive-implementation"}),
        )
        assert verdict is IntakeVerdict.IGNORE_EXCLUDED_LABEL
        assert not verdict.acts

    def test_an_excluded_label_outranks_the_admit_label(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"t3-auto", "on-hold"})),
            admit_label="t3-auto",
            exclude_labels=frozenset({"on-hold"}),
        )
        assert verdict is IntakeVerdict.IGNORE_EXCLUDED_LABEL

    def test_needs_triage_outranks_the_exclude_tier(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"needs-triage", "on-hold"})),
            admit_label="t3-auto",
            exclude_labels=frozenset({"on-hold"}),
        )
        assert verdict is IntakeVerdict.IGNORE_NEEDS_TRIAGE

    def test_the_exclude_tier_outranks_umbrella(self) -> None:
        """A row that is BOTH logs the operator's deliberate hold, not the inferred shape."""
        verdict = decide_intake(
            _facts(labels=frozenset({"on-hold"}), umbrella_reason="carries the 'epic' label"),
            admit_label="t3-auto",
            exclude_labels=frozenset({"on-hold"}),
        )
        assert verdict is IntakeVerdict.IGNORE_EXCLUDED_LABEL

    def test_the_exclude_tier_outranks_existing_work(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"on-hold"}), work_exists=True),
            admit_label="t3-auto",
            exclude_labels=frozenset({"on-hold"}),
        )
        assert verdict is IntakeVerdict.IGNORE_EXCLUDED_LABEL

    def test_an_unmatched_exclude_list_leaves_the_verdict_alone(self) -> None:
        verdict = decide_intake(
            _facts(labels=frozenset({"bug"}), author_trusted=True),
            admit_label="t3-auto",
            exclude_labels=frozenset({"on-hold"}),
        )
        assert verdict is IntakeVerdict.ACT_TRUSTED_AUTHOR

    def test_the_default_empty_policy_excludes_nothing(self) -> None:
        """An overlay that never set ``exclude_labels`` keeps its pre-#4134 verdicts."""
        verdict = decide_intake(_facts(labels=frozenset({"on-hold"}), author_trusted=True), admit_label="t3-auto")
        assert verdict is IntakeVerdict.ACT_TRUSTED_AUTHOR


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

    def test_the_exclude_policy_reaches_the_table_through_the_facade(self) -> None:
        both_forge_shapes: list[RawAPIDict] = [{"labels": [{"name": "on-hold"}]}, {"labels": ["on-hold"]}]
        for payload in both_forge_shapes:
            assert (
                decide_issue_intake(
                    payload,
                    author_trusted=True,
                    work_exists=False,
                    admit_label="t3-auto",
                    label_policy=IntakeLabelPolicy(exclude=frozenset({"on-hold"})),
                )
                is IntakeVerdict.IGNORE_EXCLUDED_LABEL
            )

    def test_an_excluded_epic_reports_the_exclusion_not_the_umbrella_shape(self) -> None:
        """Both rules reach the facade, and the operator's denylist is the one that fires."""
        assert (
            decide_issue_intake(
                {"labels": [{"name": "epic"}, {"name": "on-hold"}], "body": EPIC_BODY},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
                label_policy=IntakeLabelPolicy(exclude=frozenset({"on-hold"}), umbrella=frozenset({"epic"})),
            )
            is IntakeVerdict.IGNORE_EXCLUDED_LABEL
        )

    def test_an_epic_labelled_payload_is_declined(self) -> None:
        assert (
            decide_issue_intake(
                {"labels": [{"name": "epic"}], "body": "## Members\n"},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
                label_policy=IntakeLabelPolicy(umbrella=frozenset({"epic"})),
            )
            is IntakeVerdict.IGNORE_UMBRELLA
        )

    def test_no_configured_marker_leaves_the_label_signal_off(self) -> None:
        """The default is "none configured", never a silent copy of the shipped set."""
        assert (
            decide_issue_intake(
                {"labels": [{"name": "epic"}], "body": "one bounded fix"},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
            )
            is IntakeVerdict.ACT_TRUSTED_AUTHOR
        )

    def test_an_unlabelled_epic_shaped_payload_is_declined_structurally(self) -> None:
        assert (
            decide_issue_intake(
                {"body": EPIC_BODY},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
            )
            is IntakeVerdict.IGNORE_UMBRELLA
        )

    def test_an_unlabelled_standing_ledger_from_a_trusted_author_is_declined(self) -> None:
        """The souliane/teatree#2663 shape: no label and prose items, so only the declaration decides."""
        assert (
            decide_issue_intake(
                {"body": LEDGER_BODY},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
            )
            is IntakeVerdict.IGNORE_UMBRELLA
        )

    def test_the_umbrella_label_set_is_supplied_by_the_caller(self) -> None:
        """An overlay that renames the marker decides through its OWN set, not a constant."""
        assert (
            decide_issue_intake(
                {"labels": ["parent"]},
                author_trusted=True,
                work_exists=False,
                admit_label="t3-auto",
                label_policy=IntakeLabelPolicy(umbrella=frozenset({"parent"})),
            )
            is IntakeVerdict.IGNORE_UMBRELLA
        )


class TestPayloadBody:
    def test_reads_the_github_body_field(self) -> None:
        assert payload_body({"body": "text"}) == "text"

    def test_reads_the_gitlab_description_field(self) -> None:
        assert payload_body({"description": "text"}) == "text"

    def test_a_missing_or_null_body_is_empty(self) -> None:
        assert payload_body({}) == ""
        assert payload_body({"body": None}) == ""


class TestResolveAdmitLabel(TestCase):
    def test_defaults_to_the_shipped_t3_auto_label(self) -> None:
        assert resolve_admit_label("") == "t3-auto"

    def test_reads_the_issue_implementer_label_setting(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "admit-me")
        assert resolve_admit_label("") == "admit-me"


class TestResolveUmbrellaLabels(TestCase):
    def test_defaults_to_the_shipped_marker_set(self) -> None:
        assert resolve_umbrella_labels("") == frozenset({"epic", "umbrella", "tracking"})

    def test_reads_the_umbrella_issue_labels_setting(self) -> None:
        ConfigSetting.objects.set_value("umbrella_issue_labels", ["parent", "Roll-Up"])
        assert resolve_umbrella_labels("") == frozenset({"parent", "roll-up"})

    def test_an_explicitly_emptied_set_disables_the_label_signal(self) -> None:
        """The structural signal still stands — emptying the list is not a global off-switch."""
        ConfigSetting.objects.set_value("umbrella_issue_labels", [])
        assert resolve_umbrella_labels("") == frozenset()
