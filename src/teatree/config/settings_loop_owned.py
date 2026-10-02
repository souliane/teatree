"""One declaration base per loop, holding the settings only that loop reads.

B17 puts loop settings BELOW their loop in the cascade — a loop that is off makes its
own settings moot by construction — so which loop owns a key has to be the shape of the
config rather than a table someone keeps in step. The shape is the base a field sits in,
and the group label is the loop's own name, so a group and a loop can never come to
disagree about how they are spelled.

Membership is not a judgement call: :mod:`teatree.quality.loop_setting_ownership` derives
it from the read sites, and ``tests/quality/test_loop_settings_grouped_by_owning_loop.py``
fails when a base here and the source stop agreeing. A key read anywhere else as well —
``review_skill`` in the review gate, ``fleet_claim_enabled`` in intake — belongs to that
concern and stays where it is declared.

Split out of ``teatree.config.settings`` for the module-health LOC cap, and imported back
as declaration bases of ``UserSettings`` — see that module's docstring for why the groups
are inheritance bases rather than composed attributes.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class _ArchReviewLoopSettings:
    """How often the periodic architectural review runs, and what else re-arms it."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "arch_review")

    # #1136 / #1152 CORE always-on (not per-overlay opt-in): the cadence applies
    # uniformly to every overlay's worktrees because it is a teatree-platform behaviour.
    # The skill it dispatches is a review-gate setting and stays declared there.
    architectural_review_cadence_hours: int = 168
    architectural_review_after_merge_count: int = 25


@dataclass
class _BacklogSweepLoopSettings:
    """The daily backlog-sweep pass: whether it may close a row."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "backlog_sweep")

    # #2419 Default ON: an unattended wrong close destroys tracker signal, so the skill
    # records each fold proposal with its citation and retires a row only on approval.
    ask_before_backlog_sweep_closes: bool = True


@dataclass
class _DirectiveLoopSettings:
    """The directive intake arc's per-tick budget and its verification horizon."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "directive_loop")

    # #3649 — bounds only the INERT pre-admission arc (interpret → clarify → ratify-ask →
    # admit), which writes nothing and terminates at the human ratify gate, so a backlog
    # reaches the owner in a bounded number of ticks. Execution stays one per tick.
    directive_intake_per_tick: int = 25
    # North-star PR-7 — how long after a ratified activation the five evidence classes are
    # judged. Inert while ``directive_loop_enabled`` is off (nothing reaches VERIFYING).
    directive_verify_days: int = 7


@dataclass
class _DogfoodLoopSettings:
    """The provision-smoke pass — which overlay it exercises."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "dogfood")

    # Which overlay anchor the placeholder task is created against — empty falls back to
    # the active overlay resolved via ``discover_active_overlay``.
    dogfood_smoke_overlay: str = ""


@dataclass
class _DreamLoopSettings:
    """The nightly dream pass: which phases run, and how much one pass may spend.

    Each phase is a declared setting rather than a sub-key of the ``loops`` container,
    because ``loops`` is ALSO the loop SEED table: ``[loops.dream]`` in ``defaults.toml``
    is the dream ``Loop`` row's own definition, so one key carried two schemas and a
    stored seed table answered every phase lookup with ``None``.
    """

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "dream")

    # The six phases that only READ and rewrite memory ship ON: they file nothing and
    # spend nothing metered, so the pass is useful out of the box.
    dream_propose_evals: bool = True
    dream_cross_link: bool = True
    dream_merge: bool = True
    dream_reindex: bool = True
    dream_decay: bool = True
    # Phase 3c MEASUREMENT persists a compliance snapshot and files nothing, so the root
    # KPI is actually measured on every pass.
    dream_compliance_measure: bool = True

    # A pass batches its memory promotions into ONE ticket, so this ships ON.
    dream_memory_promote: bool = True
    # Every other phase that FILES a ticket or makes a metered model call ships OFF, so
    # opting in is a decision rather than a surprise on the first nightly pass.
    dream_derive_evals: bool = False
    dream_compliance_escalate: bool = False
    dream_automation_asks: bool = False
    # Without the live validator every clearing candidate is WITHHELD, which is the
    # nightly tick's key safety property — so this is the one that must ship OFF.
    dream_validate_live: bool = False


@dataclass
class _FollowupLoopSettings:
    """Whether the follow-up pass replies on a colleague's thread when a review resumes."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "followup")

    # Acts on colleagues, so it ships off until the owner decides.
    review_resume_reply_enabled: bool = False


@dataclass
class _HousekeepingLoopSettings:
    """The self-update pass — how often it fast-forwards, and what it does after."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "housekeeping")

    # #1249 Fast-forwards the editable teatree clone + every registered overlay clone to
    # ``origin/<default>`` once the cadence has elapsed. Hourly keeps the orchestrator
    # current without spamming the upstream remote on every tick.
    self_update_disabled: bool = False
    # ``auto_update_require_green_main`` fails closed on non-green default-branch CI.
    auto_update_require_green_main: bool = True
    # The same fast-forward over each WORK repo's main clone under ``$T3_WORKSPACE_DIR``,
    # so a clone never drifts behind after a merge and poisons a ``git show`` / ``grep``
    # investigation. Hourly keeps the clones current without spamming each remote.
    pull_main_clone_disabled: bool = False
    pull_main_clone_cadence_hours: int = 1
    # Ships off: reinstalling mid-run is the owner's call. ``T3_LOOP_AUTO_UPDATE`` env wins.
    auto_update_reinstall: bool = False


@dataclass
class _IssueDispositionLoopSettings:
    """Whether the disposition pass acts on the issues it classifies."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "issue_disposition")

    # Writes to the forge, so it ships off until the owner decides.
    auto_disposition_enabled: bool = False


@dataclass
class _IssueImplementerLoopSettings:
    """The intake scanner's own walk budget — the ceilings and labels are kill switches."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "issue_implementer")

    # Below the scan phase's 60s pool deadline on purpose: past that one the thread is
    # abandoned rather than stopped, so it keeps mutating rows after the tick ended and
    # records no resume point for the next pass (#4466).
    issue_intake_pass_budget_seconds: float = 45.0


@dataclass
class _NewsLoopSettings:
    """The news scan — how often it runs and whether it may file."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "news")

    scanning_news_cadence_hours: int = 24
    # #1391 Default ON: the skill records a ``PendingArticleSuggestion`` per candidate and
    # files an issue only on approval, because backlog pollution from unconfirmed
    # auto-filing is the failure mode this gate forecloses.
    ask_before_creating_news_tickets: bool = True


@dataclass
class _OuterLoopSettings:
    """The autoresearch runtime's bounds (guard chain G4) — inert while its flag is off."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "outer_loop")

    # T4-PR-3 — the measurement horizon after an experiment merges, the experiments
    # admitted per rolling 7-day window, and the convergence brake: after this many
    # consecutive non-KEPT decisions the loop parks itself rather than propose a fourth.
    outer_loop_measure_days: int = 7
    outer_loop_max_per_week: int = 1
    outer_loop_stop_after_consecutive_failures: int = 3


@dataclass
class _ResourcePressureLoopSettings:
    """The auto-free pass: how often it measures, and the free-byte bands it acts on."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "resource_pressure")

    # Measures ABSOLUTE reclaimable bytes (``vm_stat`` pages) — never
    # percent-of-nominal, which macOS "99 % RAM used" misleads about.
    ram_warn_avail_gb: float = 3.0
    ram_crit_avail_gb: float = 1.5
    # Measured cost of one agent in full verification over the idle baseline. The number
    # that varies by box, hence a setting: it is what one admitted ticket is sized at.
    intake_ram_per_agent_gb: float = 6.2
    # Headroom held back rather than admitted against, so a burst is absorbed instead of
    # becoming an OOM — and so tightening lowers the limit BEFORE the memory ceiling.
    intake_ram_reserve_gb: float = 4.0


@dataclass
class _ReviewLoopSettings:
    """Which authors' pull requests the review loop admits to the board."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "review")

    # #3569 Self-authored open PRs are ALWAYS admitted. COLLEAGUE / requested-reviewer PRs
    # are admitted only when this is true: the intake builds the ``ReviewerPrsScanner``
    # only when set, so false stops colleague PRs reaching the board while self-review
    # still runs. The author distinction lives HERE, upstream — the review execution is
    # blind to author.
    admit_colleague_prs_to_board: bool = True


@dataclass
class _ShipLoopSettings:
    """The forge-facing scans the ship loop runs over open merge requests."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "ship")

    # Each acts on a forge, so each ships off until the owner decides.
    gitlab_approval_scanner_enabled: bool = False
    mr_conflict_scan_enabled: bool = False
    mr_triage_enabled: bool = False


@dataclass
class _SnapshotWarmerLoopSettings:
    """When a reference-DB snapshot counts as stale enough to re-warm."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "snapshot_warmer")

    # Refreshed out-of-band so a ticket-critical-path provision never has to.
    snapshot_warmer_max_age_days: int = 1


@dataclass
class _TicketsLoopSettings:
    """The task sweep — off-switch and anti-thrash window."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "tickets")

    # #129 Verifies open teatree Task rows against their artifact's terminal state (issue
    # closed / PR merged) and completes only on durable proof, never in bulk and never on
    # a stale read.
    task_sweep_disabled: bool = False
    # The per-task window a swept task is skipped within, and the idempotency window for
    # the atomic ``last_sweep_check_ts`` stamp.
    task_sweep_recheck_interval_hours: int = 1


#: Every per-loop base, in the order its group renders under ``Loops``.
LOOP_OWNED_SETTING_BASES: tuple[type, ...] = (
    _ArchReviewLoopSettings,
    _BacklogSweepLoopSettings,
    _DirectiveLoopSettings,
    _DogfoodLoopSettings,
    _DreamLoopSettings,
    _FollowupLoopSettings,
    _HousekeepingLoopSettings,
    _IssueDispositionLoopSettings,
    _IssueImplementerLoopSettings,
    _NewsLoopSettings,
    _OuterLoopSettings,
    _ResourcePressureLoopSettings,
    _ReviewLoopSettings,
    _ShipLoopSettings,
    _SnapshotWarmerLoopSettings,
    _TicketsLoopSettings,
)
