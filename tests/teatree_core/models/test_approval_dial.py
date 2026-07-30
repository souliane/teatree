"""The #119 per-action-class approval dial: floor-first, never-fades, graduate, re-tighten.

The dial is the real thing #116's seam injected: it widens ONLY the owner-taint branch,
and only for a class the operator graduated to ``auto`` whose trailing-window metrics are
clean. The taint floor and the never-fades set are dial-independent hard floors.
"""

import pytest

from teatree.core.models import ConfigSetting, DeferredQuestion, DeferredQuestionAudit, SendAudit
from teatree.core.models.approval_dial import (
    DIAL_CONFIG_KEY,
    POLICY_RESOLVER,
    auto_answer_by_policy,
    configured_trust,
    effective_decision,
    policy_dial,
)
from teatree.core.models.approval_policy import (
    ACTION_CLASSES,
    DIRECTIVE_ADMIT,
    GATE_OR_POLICY_CHANGE,
    ON_BEHALF_POST,
    OUTER_LOOP_KEEP,
    PUBLIC_ISSUE_CREATE,
    Decision,
    approval_policy,
)
from teatree.core.models.provenance import Provenance
from teatree.core.models.trust_level import TrustLevel

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_active_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the dial reads to the global scope only, deterministic regardless of the
    # developer's ambient overlay.
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)


def _graduate(action_class: str, level: str = "auto", scope: str = "") -> None:
    stored = ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope=scope)
    table = dict(stored) if isinstance(stored, dict) else {}
    table[action_class] = level
    ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, table, scope=scope)


class TestShipsInert:
    def test_every_class_asks_by_default(self) -> None:
        for action_class in ACTION_CLASSES:
            assert policy_dial(action_class) is Decision.ASK


class TestGraduation:
    def test_a_graduated_fadeable_class_auto_approves(self) -> None:
        _graduate(OUTER_LOOP_KEEP)
        assert policy_dial(OUTER_LOOP_KEEP) is Decision.AUTO_APPROVE

    def test_configured_trust_reads_the_table(self) -> None:
        _graduate(DIRECTIVE_ADMIT)
        assert configured_trust(DIRECTIVE_ADMIT) is TrustLevel.AUTO
        assert configured_trust(ON_BEHALF_POST) is TrustLevel.ASK  # unset falls back to ASK

    def test_an_out_of_vocabulary_stored_value_fails_closed(self) -> None:
        ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, {OUTER_LOOP_KEEP: "sometimes"}, scope="")
        assert configured_trust(OUTER_LOOP_KEEP) is TrustLevel.ASK
        assert policy_dial(OUTER_LOOP_KEEP) is Decision.ASK


class TestNeverFades:
    def test_a_never_fades_class_stays_ask_even_when_stored_auto(self) -> None:
        ConfigSetting.objects.set_value(
            DIAL_CONFIG_KEY,
            {PUBLIC_ISSUE_CREATE: "auto", GATE_OR_POLICY_CHANGE: "auto"},
            scope="",
        )
        assert policy_dial(PUBLIC_ISSUE_CREATE) is Decision.ASK
        assert policy_dial(GATE_OR_POLICY_CHANGE) is Decision.ASK


class TestFloorPrecedence:
    @pytest.mark.parametrize(
        "taint",
        [Provenance.PUBLIC, Provenance.WEB, Provenance.TRUSTED_COLLEAGUE, "some-unknown-taint"],
    )
    def test_the_taint_floor_beats_a_graduated_dial(self, taint: str) -> None:
        # THE security guarantee: even a class graduated to auto is ASK for an untrusted
        # taint, because approval_policy checks the floor BEFORE the dial.
        _graduate(DIRECTIVE_ADMIT)
        _graduate(ON_BEHALF_POST)
        assert approval_policy(DIRECTIVE_ADMIT, taint, dial=policy_dial) is Decision.ASK
        assert approval_policy(ON_BEHALF_POST, taint, dial=policy_dial) is Decision.ASK

    def test_owner_taint_reaches_the_graduated_dial(self) -> None:
        _graduate(DIRECTIVE_ADMIT)
        assert approval_policy(DIRECTIVE_ADMIT, Provenance.OWNER, dial=policy_dial) is Decision.AUTO_APPROVE


class TestAutoReTighten:
    def test_a_human_decline_re_tightens_a_graduated_keep_class(self) -> None:
        _graduate(OUTER_LOOP_KEEP)
        assert policy_dial(OUTER_LOOP_KEEP) is Decision.AUTO_APPROVE  # clean window → auto
        question = DeferredQuestion.record("keep it?", options_hash="outer_loop_keep:9")
        DeferredQuestion.consume(question.pk, answer="no, revert it")  # a human decline in-window
        assert policy_dial(OUTER_LOOP_KEEP) is Decision.ASK  # breach → re-tightened

    def test_a_send_defect_escape_re_tightens_on_behalf(self) -> None:
        _graduate(ON_BEHALF_POST)
        assert policy_dial(ON_BEHALF_POST) is Decision.AUTO_APPROVE
        SendAudit.objects.create(
            channel="github",
            action="post_comment",
            mode="enforce",
            allowlist_verdict=SendAudit.Verdict.DENIED,
        )
        assert policy_dial(ON_BEHALF_POST) is Decision.ASK

    def test_redaction_rework_re_tightens_on_behalf(self) -> None:
        _graduate(ON_BEHALF_POST)
        SendAudit.objects.create(
            channel="slack",
            action="post_comment",
            mode="enforce",
            allowlist_verdict=SendAudit.Verdict.ALLOWED,
            redaction_applied=True,
        )
        assert policy_dial(ON_BEHALF_POST) is Decision.ASK

    def test_a_policy_auto_answer_is_not_counted_as_a_decline(self) -> None:
        _graduate(OUTER_LOOP_KEEP)
        question = DeferredQuestion.record("keep it?", options_hash="outer_loop_keep:9")
        auto_answer_by_policy(question, "kept")  # policy answer, not a human decline
        assert policy_dial(OUTER_LOOP_KEEP) is Decision.AUTO_APPROVE  # no breach


class TestAutoAnswerByPolicy:
    def test_consumes_single_use_with_resolved_via_policy_and_audits(self) -> None:
        question = DeferredQuestion.record("approve?", options_hash="directive_ratify:1:0")
        row = auto_answer_by_policy(question, "approve")
        assert row is not None
        assert row.answered_at is not None
        assert row.answer_text == "approve"
        assert row.resolved_via == DeferredQuestion.ResolvedVia.POLICY
        audit = DeferredQuestionAudit.objects.get(question=question)
        assert audit.action == "answered"
        assert audit.resolver_id == POLICY_RESOLVER

    def test_an_already_resolved_question_returns_none_and_writes_no_second_audit(self) -> None:
        question = DeferredQuestion.record("approve?")
        DeferredQuestion.consume(question.pk, answer="approve")
        assert auto_answer_by_policy(question, "approve") is None
        assert DeferredQuestionAudit.objects.count() == 0


class TestOverlayLayering:
    def test_an_overlay_scope_row_overrides_the_global_row(self) -> None:
        ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, {OUTER_LOOP_KEEP: "ask"}, scope="")
        ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, {OUTER_LOOP_KEEP: "auto"}, scope="acme")
        assert configured_trust(OUTER_LOOP_KEEP, overlay="acme") is TrustLevel.AUTO
        assert configured_trust(OUTER_LOOP_KEEP, overlay=None) is TrustLevel.ASK
        assert effective_decision(OUTER_LOOP_KEEP, overlay="acme") is Decision.AUTO_APPROVE
        assert effective_decision(OUTER_LOOP_KEEP, overlay=None) is Decision.ASK
