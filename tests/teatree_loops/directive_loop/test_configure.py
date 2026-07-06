"""The CONFIGURE phase — byte-identical overlay activation + instant rollback (north-star PR-7).

The one new power the loop has is bounded by ratification: the activation is written
ONLY when byte-identical to the ratified sketch (the drift guard, RED-before), and
confirmed by a real-resolver read-back. ``clear_activation`` is the reversible rollback.
"""

from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting, DeferredQuestion, Directive
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop.configure import Activation, apply_activation, clear_activation
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_SCOPE = "t3-teatree"
_KEY = "max_open_prs_per_repo_per_ticket"


def _admitted(**sketch_over: object) -> Directive:
    directive = Directive.objects.capture("max 1 MR per repo", source=Directive.Source.CLI, scope_overlay=_SCOPE)
    directive.record_interpretation(sketch_from_envelope(valid_envelope(**sketch_over)), constraint_statement="c")
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    return directive


class TestApplyActivation(TestCase):
    def test_writes_the_ratified_row_and_confirms_by_read_back(self) -> None:
        directive = _admitted()
        result = apply_activation(directive)
        assert result.applied is True
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) == 1
        assert get_effective_settings(_SCOPE).max_open_prs_per_repo_per_ticket == 1

    def test_drift_from_the_ratified_sketch_is_refused_with_no_write(self) -> None:
        # RED-before the drift guard: an activation whose value diverges from the
        # ratified sketch is refused and writes nothing.
        directive = _admitted()
        drifted = Activation(setting_key=_KEY, value=99, scope=_SCOPE)
        result = apply_activation(directive, activation=drifted)
        assert result.applied is False
        assert "drifted" in result.reason
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None

    def test_a_scope_drift_is_refused(self) -> None:
        directive = _admitted()
        drifted = Activation(setting_key=_KEY, value=1, scope="some-other-overlay")
        assert apply_activation(directive, activation=drifted).applied is False

    def test_no_sketch_is_refused(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        assert apply_activation(directive).applied is False

    def test_empty_scope_is_refused(self) -> None:
        directive = _admitted(activation_scope="")
        result = apply_activation(directive)
        assert result.applied is False
        assert "scope is empty" in result.reason

    def test_read_back_mismatch_clears_and_refuses(self) -> None:
        # A setting_key that is a valid identifier but not a real UserSettings field
        # writes, but the resolver has no such attribute → read-back mismatch → the row
        # is cleared and the activation refused (no half-applied config left behind).
        directive = _admitted(setting_key="not_a_real_setting")
        result = apply_activation(directive)
        assert result.applied is False
        assert "read-back mismatch" in result.reason
        assert ConfigSetting.objects.get_effective("not_a_real_setting", scope=_SCOPE) is None


class TestClearActivation(TestCase):
    def test_clear_rolls_back_the_row(self) -> None:
        directive = _admitted()
        apply_activation(directive)
        assert clear_activation(directive) is True
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None

    def test_clear_is_idempotent_when_nothing_is_set(self) -> None:
        directive = _admitted()
        assert clear_activation(directive) is False

    def test_clear_with_no_sketch_is_false(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        assert clear_activation(directive) is False
