"""CI-eval heal scanner (#3201 PR-3a) — flags open heal sessions, silent when idle."""

from django.test import TestCase

from teatree.core.models import CiEvalHealSession
from teatree.loop.scanners.ci_eval_heal import CI_EVAL_HEAL_ADVANCE_KIND, CiEvalHealScanner


class TestCiEvalHealScanner(TestCase):
    def test_no_open_sessions_emits_nothing(self) -> None:
        assert CiEvalHealScanner().scan() == []

    def test_open_session_emits_one_advance_signal(self) -> None:
        CiEvalHealSession.objects.open_session(overlay="teatree", pr_ref="3201-x")
        signals = CiEvalHealScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == CI_EVAL_HEAL_ADVANCE_KIND
        assert signals[0].payload["open_count"] == 1

    def test_terminal_sessions_are_not_counted(self) -> None:
        session = CiEvalHealSession.objects.open_session(overlay="teatree", pr_ref="done")
        session.trigger(ci_run_id="", head_sha="a" * 40)
        session.save()
        session.receive_result(red_scenarios=[])
        session.save()
        session.mark_green()
        session.save()
        assert CiEvalHealScanner().scan() == []

    def test_scanner_name_matches_the_loop(self) -> None:
        assert CiEvalHealScanner().name == "ci_eval_heal"
