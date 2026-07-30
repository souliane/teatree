"""Durable FSM for the CI-eval self-healing loop (#3201 PR-2).

One :class:`CiEvalHealSession` tracks one PR branch through the heal loop the
Hetzner ``t3 worker`` timer chain drives (PR-3): dispatch a behavioral-eval run
in GitHub CI, triage the machine-readable result, fix the behavioral reds, push,
re-trigger — until every scenario is GREEN, or HALT and escalate to the human
when a red cannot be greened.

Two invariants are enforced structurally on the model, not left to the loop:

* **Never suppress a red.** ``mark_green`` is guarded by :func:`_no_reds` — a
    session carrying any red scenario cannot transition to ``GREEN``. The only
    terminal states are ``GREEN`` (genuinely clean) and ``HALTED`` (escalated).
* **Fix the code, never the test.** ``record_fix`` runs the anti-cheat gate
    (``eval_heal_anticheat``, fetched from the gate registry so the model → gate
    edge stays inverted); a fix diff touching the scenario tree or the red matcher
    raises :class:`EvalHealCheatError` and the transition rolls back to ``FIXING``.

The bounded fix budget (``max_fix_attempts``) makes "un-greenable" decidable:
once :attr:`fix_budget_exhausted`, the loop halts and escalates rather than
looping forever — the FSM never provides a silent-pass path around a red.
"""

from typing import ClassVar, cast

from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition

from teatree.core.modelkit.gate_registry import get_gate

_DEFAULT_MAX_FIX_ATTEMPTS = 3


class CiEvalHealSessionManager(models.Manager["CiEvalHealSession"]):
    def open_session(
        self,
        *,
        overlay: str,
        pr_ref: str,
        head_sha: str = "",
        max_fix_attempts: int = _DEFAULT_MAX_FIX_ATTEMPTS,
    ) -> "CiEvalHealSession":
        return self.create(
            overlay=overlay,
            pr_ref=pr_ref,
            head_sha=head_sha,
            max_fix_attempts=max_fix_attempts,
        )


def _no_reds(session: object) -> bool:
    """``mark_green`` guard — true only when no red scenario remains."""
    return not cast("CiEvalHealSession", session).red_scenarios


class CiEvalHealSession(models.Model):
    """One PR branch's journey through the CI-eval self-healing loop."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        AWAITING_CI = "awaiting_ci", "Awaiting CI"
        TRIAGING = "triaging", "Triaging"
        FIXING = "fixing", "Fixing"
        PUSHED = "pushed", "Pushed"
        GREEN = "green", "Green"
        HALTED = "halted", "Halted"

    overlay = models.CharField(max_length=64)
    pr_ref = models.CharField(max_length=255)
    head_sha = models.CharField(max_length=64, blank=True, default="")
    state = FSMField(max_length=16, choices=State.choices, default=State.PENDING)
    ci_run_id = models.CharField(max_length=64, blank=True, default="")
    red_scenarios = models.JSONField(default=list, blank=True)
    fix_attempts = models.PositiveSmallIntegerField(default=0)
    max_fix_attempts = models.PositiveSmallIntegerField(default=_DEFAULT_MAX_FIX_ATTEMPTS)
    last_fix_paths = models.JSONField(default=list, blank=True)
    halt_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[CiEvalHealSessionManager] = CiEvalHealSessionManager()

    class Meta:
        db_table = "teatree_ci_eval_heal_session"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["overlay", "state"], name="ci_eval_heal_overlay_state_idx"),
        ]

    def __str__(self) -> str:
        return f"ci-eval-heal<{self.pk}:{self.pr_ref}@{self.state}>"

    @property
    def fix_budget_exhausted(self) -> bool:
        """True once no further fix attempt is allowed — the loop must halt+escalate."""
        return self.fix_attempts >= self.max_fix_attempts

    @transition(field=state, source=[State.PENDING, State.PUSHED], target=State.AWAITING_CI)
    def trigger(self, *, ci_run_id: str, head_sha: str) -> None:
        """Record the dispatched CI-eval run and start monitoring.

        Source ``[PENDING, PUSHED]`` so the first dispatch and every re-trigger
        after a pushed fix share one transition — the loop back-edge.
        """
        self.ci_run_id = ci_run_id
        self.head_sha = head_sha

    @transition(field=state, source=State.AWAITING_CI, target=State.TRIAGING)
    def receive_result(self, *, red_scenarios: list[str]) -> None:
        """Record the red-scenario subset the CI ``--summary-json`` artifact reported."""
        self.red_scenarios = list(red_scenarios)

    @transition(field=state, source=State.TRIAGING, target=State.GREEN, conditions=[_no_reds])
    def mark_green(self) -> None:
        """Terminal success — reachable ONLY when no red remains (:func:`_no_reds`)."""

    @transition(field=state, source=State.TRIAGING, target=State.FIXING)
    def begin_fix(self) -> None:
        """Start applying a code fix for the triaged behavioral reds."""

    @transition(field=state, source=State.FIXING, target=State.PUSHED)
    def record_fix(self, *, changed_paths: list[str]) -> None:
        """Record a pushed fix — refused by the anti-cheat gate if it edits the test.

        The gate runs BEFORE the attempt counter increments, so a rejected
        (cheating) fix rolls back with ``fix_attempts`` unchanged and the session
        stays in ``FIXING``.
        """
        get_gate("eval_heal_anticheat")(changed_paths)
        self.fix_attempts += 1
        self.last_fix_paths = list(changed_paths)

    @transition(field=state, source=[State.AWAITING_CI, State.TRIAGING, State.FIXING], target=State.HALTED)
    def halt(self, *, reason: str) -> None:
        """Terminal escalation — an un-greenable red or an un-retryable infra failure.

        The loop records the reason and raises a ``DeferredQuestion`` for the human
        (PR-3); the FSM never offers a silent-pass path around the red.
        """
        self.halt_reason = reason
