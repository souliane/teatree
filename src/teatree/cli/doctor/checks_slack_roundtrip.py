"""``t3 doctor`` — active Slack round-trip comms verification for headless teatree (#3411).

The "reacts-but-never-answers" detector. Headless teatree in Docker RECEIVES a
Slack DM and REACTS (👀 ack) but never POSTS an answer back — a silent half-broken
round-trip that no prior health check caught. This gate verifies the FULL comms
loop whenever a Slack backend is configured and hard-FAILs (red + non-zero doctor
exit) the moment teatree would react-but-never-answer:

1. **Outbound** — every Slack-backed overlay resolves to a real (non-no-op)
    messaging backend, so a reply CAN be posted. ``--slack-roundtrip`` additionally
    runs a LIVE ``auth.test`` per backend (proves the bot token actually reaches
    Slack, not just that config is present).
2. **Owner resolution** — the global :func:`teatree.core.notify.resolve_user_id`
    the headless egress calls resolves NON-empty. The empty-string headless failure
    (``T3_OVERLAY_NAME`` unset, no global setting) is exactly the observed bug: the
    answer pipeline silently NOOPs its reply.
3. **Inbound listener** — the Socket-Mode ``slack-listener`` singleton is live, so
    inbound events are being queued at all.
4. **Answer pipeline** — the ``inbox`` loop is enabled + unmasked and a loop worker
    is draining the queue, so a queued message actually gets answered.
5. **Evidence** — no message sits reacted-👀 but never loop-replied past the
    staleness window (the smoking gun of the observed bug, read from real traffic).

Unlike the surfacing-only Slack DM-readiness check, THIS check gates the overall
doctor exit code — a silent break must be a doctor failure, not a surprise. Slack
stays optional: with no Slack-backed overlay the check is a silent no-op.
"""

import datetime as dt
import os
from collections.abc import Callable
from dataclasses import dataclass

import typer

from teatree.cli.slack.socket_doctor import Level

#: A message reacted 👀 but never loop-replied for longer than this is the
#: "reacts-but-never-answers" signature read from real inbound traffic. Generous
#: relative to the ~20s answer cadence so a mid-flight cycle never false-alarms.
_UNANSWERED_STALENESS = dt.timedelta(minutes=5)

#: The durable ``Loop`` row that gates the reactive Slack-answer / DM-inbound work.
_ANSWER_LOOP = "inbox"


@dataclass(frozen=True, slots=True)
class RoundtripFinding:
    """One round-trip observation the doctor renders on its own line."""

    level: Level
    message: str


@dataclass(frozen=True, slots=True)
class RoundtripOutcome:
    """The full set of round-trip findings across the comms loop."""

    findings: tuple[RoundtripFinding, ...]

    @property
    def ok(self) -> bool:
        """``False`` iff any finding hard-FAILs — the value that gates the doctor exit code."""
        return not any(finding.level is Level.FAIL for finding in self.findings)


def _is_headless(env: dict[str, str]) -> bool:
    """Whether this box is a headless Docker deployment (the entrypoint always sets ``TEATREE_ROLE``).

    A missing listener is a hard FAIL only headless — that is where the operator
    steers via Slack and a down receiver silently breaks autonomy. On a plain
    interactive host (no ``TEATREE_ROLE``) it degrades to a WARN: the operator is
    at the keyboard and may simply not be running the receiver.
    """
    return bool(env.get("TEATREE_ROLE"))


def _probe_outbound(overlays: list[str], *, deep: bool) -> list[RoundtripFinding]:
    """Every Slack overlay resolves to a real backend that can post — the reply egress.

    A no-op / absent backend means the bot tokens are missing: teatree can react
    (a reaction needs no post) yet never answer. ``deep`` additionally runs a live
    ``auth.test`` so a present-but-invalid bot token is caught, not just an absent one.
    """
    from teatree.backends.messaging_noop import NoopMessagingBackend  # noqa: PLC0415 — deferred: keep import light
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: ORM-backed factory

    findings: list[RoundtripFinding] = []
    for overlay in overlays:
        backend = messaging_from_overlay(overlay)
        if backend is None or isinstance(backend, NoopMessagingBackend):
            findings.append(
                RoundtripFinding(
                    Level.FAIL,
                    f"[{overlay}] outbound Slack egress is DEAD — resolves to a no-op backend despite "
                    "messaging_backend=slack, so teatree can react 👀 but never post an answer. Bot tokens "
                    "missing at the `slack_token_ref` pass entry; run `t3 setup slack-bot`.",
                )
            )
            continue
        if deep:
            findings.append(_probe_auth_test(overlay, backend))
    return findings


def _probe_auth_test(overlay: str, backend: object) -> RoundtripFinding:
    """Live ``auth.test`` for one backend (``--slack-roundtrip`` deep mode).

    Proves the bot token actually reaches Slack and is authenticated — the active
    outbound half of the round-trip. A backend without an ``auth_test`` seam (a
    non-Slack backend that slipped through) degrades to a WARN.
    """
    auth_test = getattr(backend, "auth_test", None)
    if not callable(auth_test):
        return RoundtripFinding(Level.WARN, f"[{overlay}] backend exposes no auth.test seam — skipped live probe.")
    try:
        body = auth_test()
    except Exception as exc:  # noqa: BLE001 — a live probe error is a finding, never a crash
        return RoundtripFinding(
            Level.FAIL,
            f"[{overlay}] live Slack auth.test FAILED ({exc.__class__.__name__}: {exc}) — the bot token cannot "
            "reach/authenticate to Slack; teatree cannot post answers. Re-mint the bot token (`t3 setup slack-bot`).",
        )
    if not isinstance(body, dict) or not body.get("ok"):
        return RoundtripFinding(
            Level.FAIL,
            f"[{overlay}] live Slack auth.test returned not-ok ({body!r}) — the bot token is missing/invalid; "
            "teatree cannot post answers. Re-mint the bot token (`t3 setup slack-bot`).",
        )
    return RoundtripFinding(Level.OK, f"[{overlay}] live Slack auth.test ok (bot {body.get('user_id', '?')}).")


def _probe_owner_resolution() -> RoundtripFinding:
    """The global owner id the headless egress DMs resolves non-empty (THE observed bug).

    :func:`teatree.core.notify.resolve_user_id` is the exact resolver the headless
    worker calls with no ``T3_OVERLAY_NAME`` exported. An empty result is the
    react-but-never-answer root: the answer pipeline silently NOOPs its reply.
    """
    from teatree.core.notify import resolve_user_id  # noqa: PLC0415 — deferred: config read at call time

    if resolve_user_id():
        return RoundtripFinding(Level.OK, "owner id resolves for the headless egress (resolve_user_id non-empty).")
    return RoundtripFinding(
        Level.FAIL,
        "owner id does NOT resolve (resolve_user_id empty) — the headless answer pipeline reacts 👀 but silently "
        "NOOPs its reply. Set the owner id: `pass slack/user-id` + `t3 setup`, or set the global `slack_user_id` "
        "so the worker (no T3_OVERLAY_NAME) resolves it.",
    )


def _probe_listener(*, headless: bool) -> RoundtripFinding:
    """The Socket-Mode ``slack-listener`` singleton is live so inbound events are queued.

    Uses the SAME flock + pid path :func:`teatree.cli.slack.listen.listen_command`
    holds. A down listener means NOTHING inbound is received — a hard FAIL headless
    (autonomy is broken), a WARN on an interactive host that may not run a receiver.
    """
    from teatree.backends.slack.receiver import default_queue_path  # noqa: PLC0415 — deferred: keep import light
    from teatree.utils.singleton import flock_is_held  # noqa: PLC0415 — deferred: keep import light

    pid_path = default_queue_path().with_name("slack-listener.pid")
    if flock_is_held("slack-listener", pid_path=pid_path):
        return RoundtripFinding(Level.OK, "slack-listener receiver is live (inbound events are being queued).")
    level = Level.FAIL if headless else Level.WARN
    return RoundtripFinding(
        level,
        "slack-listener receiver is DOWN — no inbound Slack event is being received, so teatree can never answer. "
        "Restart it: `t3 slack listen` (in Docker, the `teatree-slack-listener` service).",
    )


def _probe_answer_pipeline() -> list[RoundtripFinding]:
    """The answer path is live: the ``inbox`` loop is enabled + unmasked AND a worker drains it.

    A paused/disabled/preset-masked ``inbox`` loop, or a ``loop_runner_enabled``
    kill-switch OFF, or no worker holding the flock, all leave a queued message
    forever unanswered — teatree reacts but never answers.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keep import light
    from teatree.loop.loop_state_db import loop_enabled  # noqa: PLC0415 — deferred: ORM-backed read
    from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held  # noqa: PLC0415 — deferred: keep import light

    findings: list[RoundtripFinding] = []
    if not loop_enabled(_ANSWER_LOOP):
        findings.append(
            RoundtripFinding(
                Level.FAIL,
                f"the `{_ANSWER_LOOP}` answer loop is masked (paused / disabled / preset-forced-off) — queued "
                f"messages are never answered. Enable it: `t3 loop enable {_ANSWER_LOOP}` and clear any override "
                f"(`t3 loop override {_ANSWER_LOOP} clear`).",
            )
        )
    settings = get_effective_settings()
    if not settings.loop_runner_enabled:
        findings.append(
            RoundtripFinding(
                Level.FAIL,
                "the loop runner is OFF (loop_runner_enabled=false) — no headless answer cycle runs; teatree reacts "
                "but never answers. Re-enable the loop runner.",
            )
        )
    elif not flock_is_held(WORKER_SINGLETON):
        findings.append(
            RoundtripFinding(
                Level.FAIL,
                "no loop worker holds the flock — the reactive Slack-answer cycle never runs, so queued messages are "
                "never answered. Start one: `t3 worker ensure`.",
            )
        )
    if not findings:
        findings.append(RoundtripFinding(Level.OK, "answer pipeline is live (inbox loop enabled, worker draining)."))
    return findings


def _probe_unanswered_evidence(*, now: dt.datetime | None = None) -> RoundtripFinding | None:
    """Real-traffic proof of the bug: a message reacted 👀 but never loop-replied.

    Reads :class:`PendingChatInjection` for rows that got the eyes receipt
    (``eyes_reacted_at`` set) yet were never answered/acked/delegated
    (``loop_replied_at`` and ``answered_at`` both null) beyond the staleness
    window. That is precisely reacts-but-never-answers, observed rather than
    inferred. Returns ``None`` when there is no such evidence.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: ORM import needs the registry

    cutoff = (now or timezone.now()) - _UNANSWERED_STALENESS
    stale = PendingChatInjection.objects.filter(
        eyes_reacted_at__isnull=False,
        loop_replied_at__isnull=True,
        answered_at__isnull=True,
        received_at__lt=cutoff,
    ).count()
    if stale == 0:
        return None
    return RoundtripFinding(
        Level.FAIL,
        f"reacts-but-never-answers CONFIRMED — {stale} Slack message(s) were reacted 👀 but never answered for over "
        f"{int(_UNANSWERED_STALENESS.total_seconds() // 60)}m. The answer pipeline is half-broken: check owner "
        "resolution, the loop worker, and Anthropic capacity (a masked/exhausted answer loop).",
    )


def run_slack_roundtrip_probes(
    *,
    deep: bool = False,
    env: dict[str, str] | None = None,
    now: dt.datetime | None = None,
) -> RoundtripOutcome:
    """Verify the full Slack comms loop; return the structured findings (gating).

    A silent no-op with no Slack-backed overlay (Slack is optional). ``deep`` adds
    the live ``auth.test`` outbound probe (``t3 doctor check --slack-roundtrip``).
    """
    from teatree.cli.slack.provision import _slack_overlays  # noqa: PLC0415 — deferred: ORM-backed registry read

    overlays = _slack_overlays()
    if not overlays:
        return RoundtripOutcome(findings=())

    resolved_env = env if env is not None else dict(os.environ)
    findings: list[RoundtripFinding] = []
    findings.extend(_probe_outbound(overlays, deep=deep))
    findings.extend((_probe_owner_resolution(), _probe_listener(headless=_is_headless(resolved_env))))
    findings.extend(_probe_answer_pipeline())
    evidence = _probe_unanswered_evidence(now=now)
    if evidence is not None:
        findings.append(evidence)
    return RoundtripOutcome(findings=tuple(findings))


def check_slack_roundtrip(
    *,
    deep: bool = False,
    env: dict[str, str] | None = None,
    echo: Callable[[str], object] = typer.echo,
) -> bool:
    """Run the Slack round-trip probes, render each finding, and return the pass/fail verdict.

    Gates the overall doctor exit code (unlike the surfacing-only DM-readiness
    check): a FAIL reddens the run. Crash-proof — any unexpected error degrades to
    a WARN + OK so a check bug never crashes or falsely reddens the doctor run
    (the actual comms-break signals are FAILs from probes that ran).
    """
    try:
        outcome = run_slack_roundtrip_probes(deep=deep, env=env)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        echo(f"WARN  Slack round-trip check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for finding in outcome.findings:
        echo(f"{finding.level.value:<5} Slack round-trip: {finding.message}")
    return outcome.ok


__all__ = [
    "RoundtripFinding",
    "RoundtripOutcome",
    "check_slack_roundtrip",
    "run_slack_roundtrip_probes",
]
