from typing import TYPE_CHECKING, cast

from django.db import models

from teatree.core.modelkit.gate_registry import get
from teatree.core.models.task import Task
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.repair_loop import terminal_reason_fingerprint

if TYPE_CHECKING:
    from teatree.core.cost import AttemptUsage, CostBreakdown


class TaskAttemptQuerySet(models.QuerySet):
    def headless(self) -> "TaskAttemptQuerySet":
        """Only the attempts that ran a billed detached headless-SDK run.

        SDK-equivalent billing covers headless usage only — interactive turns
        run inside the user's own session, not against the credit.
        """
        return self.filter(execution_target=Task.ExecutionTarget.HEADLESS)

    def usages(self) -> "list[AttemptUsage]":
        """Map each attempt to the :class:`AttemptUsage` the cost layer reads."""
        AttemptUsage = cast("type[AttemptUsage]", get("cost", "AttemptUsage"))  # noqa: N806 — PascalCase binds a runtime-resolved model class, matching its class name

        return [
            AttemptUsage(
                model=row.model or None,
                reported_cost_usd=row.cost_usd,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                cache_read_tokens=row.cache_read_tokens or 0,
                cache_write_tokens=row.cache_write_tokens or 0,
                lane=row.lane,
                estimated=row.cost_is_estimated,
                phase=row.task.phase,
            )
            for row in self.select_related("task").only(
                "model",
                "cost_usd",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "lane",
                "cost_is_estimated",
                "task__phase",
            )
        ]

    def cost_breakdown(self) -> "CostBreakdown":
        """SDK-equivalent spend across the attempts in this queryset."""
        CostBreakdown = cast("type[CostBreakdown]", get("cost", "CostBreakdown"))  # noqa: N806 — PascalCase binds a runtime-resolved model class, matching its class name

        return CostBreakdown.from_usages(self.usages())


class TaskAttempt(models.Model):
    class Lane(models.TextChoices):
        """The Layer-2 lane (souliane/teatree#2887) an attempt authenticated through.

        ``""`` (blank, the field default) means unattributed — no explicit
        ``agent_harness_provider`` pin was configured for the dispatch, so the
        ambient-credential default authenticated however the ``claude`` CLI's
        own login state resolved, which is unobservable from here.
        """

        SUBSCRIPTION = "subscription", "Subscription"
        METERED = "metered", "Metered"

    class Outcome(models.TextChoices):
        """The terminal classification of a finished attempt (souliane/teatree#16).

        The explicit discriminator that replaces inferring success/failure from an
        overloaded ``exit_code`` + ``error``: an envelope refusal is recorded with
        ``exit_code=0`` AND a non-empty ``error``, so any reader keying on
        ``exit_code`` alone counts a refusal as a clean success. ``outcome`` is
        stamped on every :meth:`save` from the current fields, so a consumer (the
        S5 repair-burn signal) reads a first-class terminal state instead of
        re-deriving it. Blank (``""``) is an attempt still in flight — no
        ``exit_code`` recorded yet — and is neither a success nor a failure.
        """

        SUCCESS = "success", "Success"
        REFUSAL = "refusal", "Refusal"
        CRASH = "crash", "Crash"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    execution_target = models.CharField(max_length=32, choices=Task.ExecutionTarget.choices)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    artifact_path = models.CharField(max_length=500, blank=True)
    result = models.JSONField(default=dict, blank=True)
    model = models.CharField(max_length=128, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cache_read_tokens = models.IntegerField(null=True, blank=True)
    cache_write_tokens = models.IntegerField(null=True, blank=True)
    # Float (not Decimal) is a recorded, accepted waiver of the float-for-money
    # anti-pattern: this is provider-cost telemetry, never invoicing. See the
    # ``float-for-money`` entry in src/teatree/quality/antipatterns.yaml.
    cost_usd = models.FloatField(null=True, blank=True)
    # #3157 E5: whether ``cost_usd`` is a price-table ESTIMATE (True) or a real
    # reported figure — the CLI/SDK ``total_cost_usd`` or the metered router's own
    # reported cost passed through. Default True so a historical row whose provenance
    # is unknown is flagged conservatively as an estimate, never presented as a vetted
    # billed figure. ``t3 cost`` annotates estimated spend so a router-lane run's real
    # cost is distinguishable from a price-table guess.
    cost_is_estimated = models.BooleanField(default=True)
    num_turns = models.IntegerField(null=True, blank=True)
    launch_url = models.URLField(max_length=500, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)
    # souliane/teatree#657: the Layer-2 lane this attempt's tokens are
    # attributable to, so #2565's two-lane cost strategy is observable.
    lane = models.CharField(max_length=16, choices=Lane.choices, blank=True, default="")
    # #2009 repair-loop budget: 1-based attempt number for this attempt's
    # (ticket, normalized-phase), spanning re-queued Task rows. Auto-stamped on
    # insert; 0 only on a transient unsaved instance.
    iteration = models.PositiveIntegerField(default=0)
    # #2009 stall detection: stable hash of this attempt's terminal reason
    # (its ``error``), normalized so transient noise does not defeat the
    # identical-failure check. Empty for a clean (non-failing) attempt.
    error_fingerprint = models.CharField(max_length=64, blank=True, default="")
    # #16: the explicit success/refusal/crash discriminator, stamped from
    # exit_code + error on every save (see _classify_outcome). Blank while the
    # attempt is still in flight (no exit_code yet).
    outcome = models.CharField(max_length=16, choices=Outcome.choices, blank=True, default="")
    # #3673 Tier 3: dispatch provenance the drawer surfaces alongside model/lane.
    # reasoning_effort is the per-tier effort the spawn resolved (an EFFORT_SCALE
    # member, or blank when the tier inherits the SDK default); skills_loaded is
    # the resolved skill-bundle name list. Captured going forward only — a
    # historical row keeps the blank/empty defaults, never backfilled.
    reasoning_effort = models.CharField(max_length=16, blank=True, default="")
    skills_loaded = models.JSONField(default=list, blank=True)

    objects = TaskAttemptQuerySet.as_manager()

    class Meta:
        db_table = "teatree_taskattempt"

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self._state.adding:
            self._stamp_repair_loop_fields()
        self.outcome = self._classify_outcome()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def _classify_outcome(self) -> str:
        """Derive the terminal outcome from ``exit_code`` + ``error`` (#16).

        The single classification rule every reader shares: a genuine success is
        ``exit_code == 0`` with NO error; an envelope refusal is ``exit_code == 0``
        WITH an error; any non-zero exit is a crash. A ``None`` exit_code is an
        attempt still in flight — left blank, classified as neither. Recomputed on
        every save (not just insert) because the terminal fields are typically
        written when the attempt completes, after the in-flight row was inserted.
        """
        if self.exit_code is None:
            return ""
        if self.exit_code == 0:
            return self.Outcome.REFUSAL if self.error else self.Outcome.SUCCESS
        return self.Outcome.CRASH

    @property
    def effective_tokens(self) -> float | None:
        """GitHub's ET formula for this attempt (souliane/teatree#657): ``m*(1*I + 0.1*C + 4*O)``.

        ``None`` when no token counts were ever captured (the run never
        reached a billed SDK turn) — mirrors ``cost_usd``'s null-when-uncaptured
        contract rather than reporting a misleading 0.
        """
        if self.input_tokens is None and self.output_tokens is None and self.cache_read_tokens is None:
            return None
        AttemptUsage = cast("type[AttemptUsage]", get("cost", "AttemptUsage"))  # noqa: N806 — PascalCase binds a runtime-resolved model class, matching its class name
        return AttemptUsage(
            model=self.model or None,
            reported_cost_usd=self.cost_usd,
            input_tokens=self.input_tokens or 0,
            output_tokens=self.output_tokens or 0,
            cache_read_tokens=self.cache_read_tokens or 0,
            cache_write_tokens=self.cache_write_tokens or 0,
            lane=self.lane,
        ).effective_tokens

    def _stamp_repair_loop_fields(self) -> None:
        """Stamp the iteration counter + error fingerprint on insert (#2009).

        The single chokepoint every attempt-creation site funnels through, so the
        budget fields cannot drift between the headless recorder, the in-session
        recorder, and the operator out-of-band paths. Each is stamped only when
        unset, so an explicit value (a backfill or a test) is never clobbered.
        """
        if not self.error_fingerprint:
            self.error_fingerprint = terminal_reason_fingerprint(self.error)
        # A limit-park records a scheduling event, not a work iteration (#3689): the
        # budget query (task_repair.phase_attempts) already EXCLUDES park rows, so
        # stamping one here disagrees with that query and leaves the park row carrying a
        # bogus work-iteration number that corrupts the "attempt N/max" display and the
        # S5 signal. Leave it at the 0 sentinel so the stamp and the budget query agree.
        if not self.iteration and self.task_id and not self.error.startswith(LIMIT_PARKED_PREFIX):  # ty: ignore[unresolved-attribute]
            self.iteration = self.task.phase_iteration_count() + 1
