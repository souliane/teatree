# test-path: cross-cutting
"""The round-trip gate must probe the seam the RUNTIME uses, and read the ledger back.

Two holes let a green ``t3 doctor`` coexist with total notification failure for a
day: every outbound probe resolved backends BY NAME (which the headless egress never
does — it resolves ambiently with no ``T3_OVERLAY_NAME``), and nothing ever read the
``BotPing`` ledger, where 264 dropped owner DMs were sitting in plain sight.
"""

import contextlib
import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.cli.doctor.checks_slack_roundtrip import Level, run_slack_roundtrip_probes
from teatree.core.models import BotPing, Loop
from teatree.loops.seed import seed_default_loops_and_prompts


class _FakeSlackBackend:
    def auth_test(self) -> dict[str, object]:
        return {"ok": True, "user_id": "BOT1"}


def _fails(outcome) -> str:
    return " || ".join(f.message for f in outcome.findings if f.level is Level.FAIL)


class _RoundtripBaseline(TestCase):
    """A round-trip that is healthy in every respect the gate already covered."""

    def setUp(self) -> None:
        Loop.objects.filter(name="inbox").delete()
        seed_default_loops_and_prompts()
        Loop.objects.filter(name="inbox").update(enabled=True)
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patch("teatree.cli.slack.provision._slack_overlays", return_value=["acme"]))
        self._stack.enter_context(patch("teatree.core.notify.resolve_user_id", return_value="U_OWNER"))
        self._stack.enter_context(patch("teatree.utils.singleton.flock_is_held", side_effect=lambda *_a, **_k: True))
        self.addCleanup(self._stack.close)

    def _with_messaging(self, *, by_name: object, ambient: object) -> None:
        """Resolve one backend when asked BY NAME and another when asked ambiently.

        The shape that hides a dead transport: ``messaging_from_overlay(<name>)``
        returns a real Slack backend while the ambient egress resolves nothing,
        so a by-name health check passes while nothing can be delivered. The
        ambient probe rides ``resolve_owner_dm_backend`` (the runtime's own
        seam), so both of its tiers are pinned here: the active-overlay tier to
        *ambient*, the sole-credentialed fallback to empty.
        """

        def resolve(name: str | None = None) -> object:
            return by_name if name else ambient

        self._stack.enter_context(patch("teatree.core.backend_factory.messaging_from_overlay", side_effect=resolve))
        self._stack.enter_context(patch("teatree.core.notify.messaging_from_overlay", side_effect=resolve))
        self._stack.enter_context(
            patch("teatree.core.backend_factory.OwnerMessagingTransport.credentialed_backends", return_value=[])
        )


class TestAmbientEgressProbe(_RoundtripBaseline):
    def test_fails_when_the_headless_seam_is_dead_but_the_named_one_resolves(self) -> None:
        self._with_messaging(by_name=_FakeSlackBackend(), ambient=None)
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "ambient egress is DEAD" in _fails(outcome)

    def test_fails_when_the_headless_seam_resolves_only_a_noop_backend(self) -> None:
        self._with_messaging(by_name=_FakeSlackBackend(), ambient=NoopMessagingBackend())
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "ambient egress is DEAD" in _fails(outcome)

    def test_passes_when_the_headless_seam_resolves_a_real_backend(self) -> None:
        self._with_messaging(by_name=_FakeSlackBackend(), ambient=_FakeSlackBackend())
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok, _fails(outcome)


class TestUndeliveredOwnerNotificationEvidence(_RoundtripBaseline):
    def setUp(self) -> None:
        super().setUp()
        self._with_messaging(by_name=_FakeSlackBackend(), ambient=_FakeSlackBackend())

    def _record(self, *, status: str, audience: str = "owner_delivery", key: str = "dropped-1") -> None:
        BotPing.objects.create(
            idempotency_key=key,
            kind=BotPing.Kind.INFO,
            status=status,
            audience=audience,
            text="review of the tracked MR is done",
            error_message="no messaging backend configured",
            posted_at=timezone.now(),
        )

    def test_fails_when_an_owner_notification_was_dropped_in_the_window(self) -> None:
        self._record(status=BotPing.Status.NOOP)
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "NEVER REACHED THE OWNER" in _fails(outcome)
        assert "no messaging backend configured" in _fails(outcome)

    def test_fails_on_an_expired_drop_too_since_the_owner_still_never_heard(self) -> None:
        self._record(status=BotPing.Status.EXPIRED)
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert not outcome.ok
        assert "NEVER REACHED THE OWNER" in _fails(outcome)

    def test_stays_green_when_the_notification_was_delivered(self) -> None:
        self._record(status=BotPing.Status.SENT)
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok, _fails(outcome)

    def test_ignores_an_internal_audience_drop_which_is_never_meant_to_be_dmd(self) -> None:
        self._record(status=BotPing.Status.LOGGED, audience="internal")
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok, _fails(outcome)

    def test_ignores_a_drop_older_than_the_window(self) -> None:
        self._record(status=BotPing.Status.NOOP)
        BotPing.objects.filter(idempotency_key="dropped-1").update(
            posted_at=timezone.now() - dt.timedelta(days=3),
        )
        outcome = run_slack_roundtrip_probes(env={"TEATREE_ROLE": "worker"})
        assert outcome.ok, _fails(outcome)
