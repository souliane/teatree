# test-path: cross-cutting
"""``t3 doctor`` Slack round-trip comms gate (#3411) — the reacts-but-never-answers detector.

Functional coverage of :mod:`teatree.cli.doctor.checks_slack_roundtrip`: seeds real
``Loop`` / ``PendingChatInjection`` rows in the test DB and stubs only the genuinely
external seams (the config-resolution resolvers, the process-liveness flock, the
overlay backend factory, the live ``auth.test``). Every scenario asserts on the
structured :class:`RoundtripOutcome` and its ``ok`` verdict — the value that gates
the overall doctor exit code.
"""

import contextlib
import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.cli.doctor.checks_slack_roundtrip import Level, check_slack_roundtrip, run_slack_roundtrip_probes
from teatree.core.models import Loop, PendingChatInjection
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.utils.singleton import WORKER_SINGLETON


class _FakeSlackBackend:
    """A non-no-op messaging backend with a controllable ``auth.test`` seam."""

    def __init__(self, auth_body: object = None, auth_raises: Exception | None = None) -> None:
        self._auth_body = auth_body if auth_body is not None else {"ok": True, "user_id": "BOT1"}
        self._auth_raises = auth_raises

    def auth_test(self) -> object:
        if self._auth_raises is not None:
            raise self._auth_raises
        return self._auth_body


def _messages(outcome) -> str:
    return " || ".join(f.message for f in outcome.findings)


class _HealthyBaseline(TestCase):
    """Base with a fully-healthy round-trip; each subclass breaks exactly one seam."""

    def setUp(self) -> None:
        # Recreate the `inbox` answer loop from the production seed SSOT so the row is
        # valid against EVERY Loop CHECK constraint by construction (loop_prompt_xor_script,
        # loop_script_requires_delay, and any future one) — a polluter test in the shuffled
        # collection clears Loop rows (`Loop.objects.all().delete()`), dropping the
        # migration-seeded inbox, so this setUp must not depend on it surviving. Delete-first
        # then reseed also repairs a mutated row; then force ENABLED with no LoopState hold so
        # loop_enabled("inbox") is True.
        Loop.objects.filter(name="inbox").delete()
        seed_default_loops_and_prompts()
        Loop.objects.filter(name="inbox").update(enabled=True)
        self._backend = _FakeSlackBackend()
        self._stack = contextlib.ExitStack()
        self.overlays = self._stack.enter_context(
            patch("teatree.cli.slack.provision._slack_overlays", return_value=["acme"])
        )
        self.messaging = self._stack.enter_context(
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self._backend)
        )
        self.resolve_user_id = self._stack.enter_context(
            patch("teatree.core.notify.resolve_user_id", return_value="U_OWNER")
        )
        # A live listener AND a live worker both hold their flocks by default.
        self.flock = self._stack.enter_context(
            patch("teatree.utils.singleton.flock_is_held", side_effect=lambda *_a, **_k: True)
        )
        self.addCleanup(self._stack.close)


class TestHealthyRoundtrip(_HealthyBaseline):
    def test_all_ok_and_gate_passes(self) -> None:
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok
        assert all(f.level is Level.OK for f in outcome.findings), _messages(outcome)


class TestGatingSkip(TestCase):
    def test_no_slack_overlay_is_a_silent_noop(self) -> None:
        with patch("teatree.cli.slack.provision._slack_overlays", return_value=[]):
            outcome = run_slack_roundtrip_probes()
        assert outcome.ok
        assert outcome.findings == ()


class TestOutboundEgress(_HealthyBaseline):
    def test_noop_backend_fails_loudly(self) -> None:
        self.messaging.return_value = NoopMessagingBackend()
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "outbound Slack egress is DEAD" in _messages(outcome)


class TestOwnerResolution(_HealthyBaseline):
    def test_empty_owner_id_is_the_reacts_but_never_answers_root(self) -> None:
        self.resolve_user_id.return_value = ""
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "resolve_user_id empty" in _messages(outcome)


class TestListenerLiveness(_HealthyBaseline):
    def _listener_down(self, name: str, *_a, **_k) -> bool:
        return name != "slack-listener"

    def test_listener_down_is_hard_fail_headless(self) -> None:
        self.flock.side_effect = self._listener_down
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "slack-listener"})
        assert not outcome.ok
        assert "slack-listener receiver is DOWN" in _messages(outcome)

    def test_listener_down_degrades_to_warn_on_interactive_host(self) -> None:
        self.flock.side_effect = self._listener_down
        outcome = run_slack_roundtrip_probes(env={})
        assert outcome.ok  # a WARN, not a hard FAIL, off a headless deployment
        assert any(f.level is Level.WARN and "receiver is DOWN" in f.message for f in outcome.findings)


class TestAnswerPipeline(_HealthyBaseline):
    def test_masked_inbox_loop_fails(self) -> None:
        Loop.objects.filter(name="inbox").update(enabled=False)
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "answer loop is masked" in _messages(outcome)

    def test_loop_runner_off_fails(self) -> None:
        with patch("teatree.config.get_effective_settings") as settings:
            settings.return_value.loop_runner_enabled = False
            outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "loop runner is OFF" in _messages(outcome)

    def test_no_worker_flock_fails(self) -> None:
        self.flock.side_effect = lambda name, *_a, **_k: name != WORKER_SINGLETON
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "no loop worker holds the flock" in _messages(outcome)


class TestUnansweredEvidence(_HealthyBaseline):
    def test_stale_reacted_but_unanswered_message_confirms_the_bug(self) -> None:
        old = timezone.now() - dt.timedelta(minutes=30)
        PendingChatInjection.objects.create(
            overlay="acme",
            channel="D1",
            slack_ts="1.1",
            text="please answer this",
            received_at=old,
            eyes_reacted_at=old,
        )
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "reacts-but-never-answers CONFIRMED" in _messages(outcome)

    def test_recently_reacted_message_does_not_false_alarm(self) -> None:
        now = timezone.now()
        PendingChatInjection.objects.create(
            overlay="acme",
            channel="D1",
            slack_ts="2.2",
            text="please answer this",
            received_at=now,
            eyes_reacted_at=now,
        )
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok

    def test_answered_message_is_not_flagged(self) -> None:
        old = timezone.now() - dt.timedelta(minutes=30)
        PendingChatInjection.objects.create(
            overlay="acme",
            channel="D1",
            slack_ts="3.3",
            text="please answer this",
            received_at=old,
            eyes_reacted_at=old,
            loop_replied_at=old,
        )
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok


class TestDeepRoundtrip(_HealthyBaseline):
    def test_live_auth_test_ok(self) -> None:
        outcome = run_slack_roundtrip_probes(deep=True, env={"TEATREE_ROLE": "worker"})
        assert outcome.ok
        assert any("live Slack auth.test ok" in f.message for f in outcome.findings)

    def test_live_auth_test_not_ok_fails(self) -> None:
        self._backend._auth_body = {"ok": False, "error": "invalid_auth"}
        outcome = run_slack_roundtrip_probes(deep=True, env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "live Slack auth.test returned not-ok" in _messages(outcome)

    def test_live_auth_test_raise_fails(self) -> None:
        self._backend._auth_raises = RuntimeError("boom")
        outcome = run_slack_roundtrip_probes(deep=True, env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "live Slack auth.test FAILED" in _messages(outcome)

    def test_default_mode_skips_the_live_probe(self) -> None:
        outcome = run_slack_roundtrip_probes(deep=False, env={"TEATREE_ROLE": "worker"})
        assert not any("auth.test" in f.message for f in outcome.findings)


class TestRenderAndGate(_HealthyBaseline):
    def test_check_renders_each_finding_and_returns_verdict(self) -> None:
        self.resolve_user_id.return_value = ""
        lines: list[str] = []
        ok = check_slack_roundtrip(env={"TEATREE_ROLE": "worker"}, echo=lines.append)
        assert ok is False
        assert any("Slack round-trip:" in line and "FAIL" in line for line in lines)

    def test_check_is_crash_proof(self) -> None:
        lines: list[str] = []
        with patch(
            "teatree.cli.slack.provision._slack_overlays",
            side_effect=RuntimeError("boom"),
        ):
            ok = check_slack_roundtrip(echo=lines.append)
        assert ok is True  # a check bug degrades to OK, never crashes/reddens the run
        assert any("crashed" in line for line in lines)
