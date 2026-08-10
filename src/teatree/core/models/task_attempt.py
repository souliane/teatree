from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.db import models
from django.db.models.functions import Coalesce

from teatree.core.modelkit.gate_registry import get
from teatree.core.modelkit.task_failure_taxonomy import FailureKind, classify_failure
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.repair_loop import terminal_reason_fingerprint

if TYPE_CHECKING:
    from teatree.core.cost import AttemptUsage, CostBreakdown, UsageGroup


class TaskAttemptQuerySet(models.QuerySet):
    def create(self, **kwargs: object) -> "TaskAttempt":
        """Record an attempt — folding a REPEATED, unchanged usage-window park into one row.

        A limit-park is a scheduling event on an unchanged condition, and every writer that
        re-derives it (the admission guard, the all-accounts-exhausted park) runs on a
        poll cadence, so appending one audit row per poll narrates a static state forever:
        the measured residue was 338,741 park rows against 1,203 real dispatches, in a
        1.2 GB control DB. When the task's most recent attempt is a park carrying the
        IDENTICAL reason, this bumps that row's ``park_repeats`` and refreshes its
        ``ended_at`` instead — one row saying "still parked, N polls later".

        A changed reason, an intervening real attempt, and any non-park attempt all insert
        normally, so the audit trail still shows every genuine transition.
        """
        coalesced = self._coalesce_repeated_park(
            task=kwargs.get("task"),
            error=kwargs.get("error"),
            ended_at=kwargs.get("ended_at"),
        )
        return coalesced if coalesced is not None else super().create(**kwargs)

    def _coalesce_repeated_park(self, *, task: object, error: object, ended_at: object) -> "TaskAttempt | None":
        """The preceding park row this insert repeats verbatim, bumped — or ``None`` to insert."""
        if task is None or not isinstance(error, str) or not error.startswith(LIMIT_PARKED_PREFIX):
            return None
        latest = self.model.objects.filter(task=task).order_by("-started_at", "-pk").first()
        if latest is None or latest.error != error:
            return None
        self.model.objects.filter(pk=latest.pk).update(
            park_repeats=models.F("park_repeats") + 1,
            ended_at=ended_at if isinstance(ended_at, datetime) else latest.ended_at,
        )
        latest.refresh_from_db()
        return latest

    def prunable(self, cutoff: datetime) -> "TaskAttemptQuerySet":
        """Attempts safe to delete (#3693): the conservative double guard.

        An attempt is prunable ONLY when it started before *cutoff* AND its owning task
        is terminal AND that task's ticket is definitively finished. SHIPPED is NOT
        finished (its PR is still open, so the ticket may take review comments and
        re-work), so ``marker_release_states()`` plus RETROSPECTED is the terminal set.
        An attempt of an active task, or of a live ticket, is NEVER prunable — deleting
        a referenced/in-flight row is far worse than a bloated DB.
        """
        finished = Ticket.marker_release_states() | {Ticket.State.RETROSPECTED}
        return self.filter(
            started_at__lt=cutoff,
            task__status__in=Task.Status.terminal(),
            task__ticket__state__in=finished,
        )

    def prunable_parks(self, cutoff: datetime) -> "TaskAttemptQuerySet":
        """Park-audit rows safe to delete — the lane :meth:`prunable` structurally cannot reach.

        :meth:`prunable`'s terminal-owned double guard is the right protection for a
        REAL attempt and the wrong question for a park: ``usage_window._record_park``
        returns the task to the queue PENDING, so a park row's owning task is by
        construction NON-terminal and no park row is ever a candidate there. That is
        why a park-bloated table reports "would prune 0" — the sanctioned remedy is a
        guaranteed no-op on exactly the rows that bloated it.

        This lane asks the park's own three questions instead.

        **Is it a park?** ``error`` carries :data:`LIMIT_PARKED_PREFIX` — the ONE
        canonical marker, written by the single ``_record_park`` chokepoint and
        already the definition the coalescer, the iteration stamp and the park-spin
        detector share. An exact prefix, not a shape heuristic: a ``stuck_loop:``
        lease-loss breach is a genuine watchdog failure record, and is deliberately
        NOT swept in with it.

        **Does it carry billed telemetry?** Never delete a row holding a token or
        cost figure. A park records none by construction, so this can only fire on a
        future writer or a marker collision — which is the point: the priced rows are
        the entire cost ledger, and no marker-string change may put them in reach.

        **Is it stale?** Measured on ``ended_at``, the LAST time the park was
        observed — not ``started_at``. A repeated park folds into ONE row whose
        ``started_at`` stays ancient while ``ended_at`` refreshes each poll, so a
        ``started_at`` window would delete exactly the live "still parked, N polls
        later" row. ``started_at`` is the fallback when ``ended_at`` was never set.
        """
        return self.filter(
            error__startswith=LIMIT_PARKED_PREFIX,
            input_tokens__isnull=True,
            output_tokens__isnull=True,
            cache_read_tokens__isnull=True,
            cache_write_tokens__isnull=True,
            cost_usd__isnull=True,
        ).filter(
            models.Q(ended_at__lt=cutoff) | models.Q(ended_at__isnull=True, started_at__lt=cutoff),
        )

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

    def usage_groups(self) -> "list[UsageGroup]":
        """Token totals per costing key, aggregated by the database.

        One row per distinct ``(model, lane, phase, estimated, reported?)`` — a
        handful of rows however many attempts the queryset spans. ``reported?``
        must be part of the key: a group mixing attempts that reported a cost with
        attempts that did not cannot be priced as one bucket.
        """
        UsageGroup = cast("type[UsageGroup]", get("cost", "UsageGroup"))  # noqa: N806 — PascalCase binds a runtime-resolved model class, matching its class name

        rows = (
            self.annotate(cost_reported=models.Q(cost_usd__isnull=False))
            .values("model", "lane", "task__phase", "cost_is_estimated", "cost_reported")
            .annotate(
                reported_total=models.Sum("cost_usd"),
                input_total=Coalesce(models.Sum("input_tokens"), 0),
                output_total=Coalesce(models.Sum("output_tokens"), 0),
                cache_read_total=Coalesce(models.Sum("cache_read_tokens"), 0),
                cache_write_total=Coalesce(models.Sum("cache_write_tokens"), 0),
                attempt_count=models.Count("pk"),
            )
            .order_by()
        )
        return [
            UsageGroup(
                model=row["model"] or None,
                lane=row["lane"],
                phase=row["task__phase"],
                estimated=row["cost_is_estimated"],
                reported_cost_usd=row["reported_total"] if row["cost_reported"] else None,
                input_tokens=row["input_total"],
                output_tokens=row["output_total"],
                cache_read_tokens=row["cache_read_total"],
                cache_write_tokens=row["cache_write_total"],
                attempts=row["attempt_count"],
            )
            for row in rows
        ]

    def cost_breakdown(self) -> "CostBreakdown":
        """SDK-equivalent spend across the attempts in this queryset.

        Aggregated by the database (:meth:`usage_groups`), never by instantiating
        each attempt: the cycle-to-date chip spans the whole table, and walking
        340k rows through the ORM was the 19s the health page took to render.
        """
        CostBreakdown = cast("type[CostBreakdown]", get("cost", "CostBreakdown"))  # noqa: N806 — PascalCase binds a runtime-resolved model class, matching its class name

        return CostBreakdown.from_groups(self.usage_groups())


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
    # #3957: the NAMED cause of this attempt's failure, stamped from ``error`` on every
    # save (see _classify_failure_kind). ``outcome`` says whether the attempt failed;
    # this says WHY, in the shared FailureKind vocabulary — so failures are groupable by
    # cause ("how many of these are lost leases vs real review defects?") instead of
    # needing every reader to pattern-match free text. Blank on a clean attempt.
    failure_kind = models.CharField(max_length=32, choices=FailureKind.choices, blank=True, default="")
    # #3673 Tier 3: dispatch provenance the drawer surfaces alongside model/lane.
    # reasoning_effort is the per-tier effort the spawn resolved (an EFFORT_SCALE
    # member, or blank when the tier inherits the SDK default); skills_loaded is
    # the resolved skill-bundle name list. Captured going forward only — a
    # historical row keeps the blank/empty defaults, never backfilled.
    reasoning_effort = models.CharField(max_length=16, blank=True, default="")
    skills_loaded = models.JSONField(default=list, blank=True)
    # How many further polls re-derived this row's unchanged ``limit_parked:`` reason
    # after it was written (see TaskAttemptQuerySet.create). 0 on every non-park row and
    # on a park observed once, so "how long has this been parked" is a field read rather
    # than a row count over an unbounded append log.
    park_repeats = models.PositiveIntegerField(default=0)

    objects = TaskAttemptQuerySet.as_manager()

    class Meta:
        db_table = "teatree_taskattempt"
        indexes = (
            # Carries every column :meth:`TaskAttemptQuerySet.usage_groups` reads, so the
            # cycle-to-date spend aggregate is answered from a ~20 MB index instead of
            # scanning a table whose rows hold the (large) error / result / skills_loaded
            # payloads — `EXPLAIN QUERY PLAN` reports `SEARCH USING COVERING INDEX`. The
            # cycle filter leads so the range is the seek; the rest is what makes it cover.
            models.Index(
                fields=[
                    "started_at",
                    "model",
                    "lane",
                    "cost_is_estimated",
                    "cost_usd",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "task",
                ],
                name="taskattempt_cost_cover",
            ),
        )

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self._state.adding:
            self._stamp_repair_loop_fields()
        self.outcome = self._classify_outcome()
        self.failure_kind = self._classify_failure_kind()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def _classify_failure_kind(self) -> str:
        """Name this attempt's failure cause from ``error`` (#3957), blank when it did not fail.

        Keyed on ``error`` rather than ``exit_code`` for the same reason ``outcome`` is
        not: an envelope refusal is recorded with ``exit_code=0`` AND a non-empty error,
        so an ``exit_code``-only reader would classify a refusal as a clean run. An
        attempt still in flight has no error yet and is left blank.
        """
        if not self.error.strip():
            return ""
        return classify_failure(self.error)

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
