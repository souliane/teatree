"""One declaration base per loop, holding the settings only that loop reads.

B17 puts loop settings BELOW their loop in the cascade — a loop that is off makes its
own settings moot by construction — so which loop owns a key has to be the shape of the
config rather than a table someone keeps in step. The shape is the base a field sits in,
and the group label is the loop's own name, so a group and a loop can never come to
disagree about how they are spelled.

Membership is not a judgement call: :mod:`teatree.quality.loop_setting_ownership` derives
it from the read sites, and ``tests/quality/test_loop_settings_grouped_by_owning_loop.py``
fails when a base here and the source stop agreeing. A key read anywhere else as well —
``review_skill`` in the review gate — belongs to that concern and stays where it
is declared.

Split out of ``teatree.config.settings`` for the module-health LOC cap, and imported back
as declaration bases of ``UserSettings`` — see that module's docstring for why the groups
are inheritance bases rather than composed attributes.
"""

import logging
from dataclasses import dataclass
from typing import ClassVar

from teatree.config.declaration import Category, SettingSpec, setting
from teatree.config.setting_parsers import (
    _parse_str_list,
    _parse_strict_bool,
    _parse_strict_float,
    _parse_strict_int,
    _parse_strict_str,
)

logger = logging.getLogger(__name__)
MAX_SELF_IMPROVE_OWNER_WAIT_MINUTES = 1440


def parse_self_improve_owner_wait_minutes(raw: object) -> int:
    if type(raw) is int and 1 <= raw <= MAX_SELF_IMPROVE_OWNER_WAIT_MINUTES:
        return raw
    logger.error("Invalid self_improve_owner_wait_minutes %r; using 60", raw)
    return 60


@dataclass
class _ArchReviewLoopSettings:
    """How often the periodic architectural review runs, and what else re-arms it."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "arch_review")

    # #1136 / #1152 CORE always-on (not per-overlay opt-in): the cadence applies
    # uniformly to every overlay's worktrees because it is a teatree-platform behaviour.
    # The skill it dispatches is a review-gate setting and stays declared there.
    architectural_review_cadence_hours: int = setting(
        168, spec=SettingSpec(help="hours between architectural review runs", parse=_parse_strict_int)
    )
    architectural_review_after_merge_count: int = setting(
        25,
        spec=SettingSpec(
            help="merges that must land before another architectural review is due", parse=_parse_strict_int
        ),
    )


@dataclass
class _BacklogSweepLoopSettings:
    """The daily backlog-sweep pass: whether it may close a row."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "backlog_sweep")

    # #2419 Default ON: an unattended wrong close destroys tracker signal, so the skill
    # records each fold proposal with its citation and retires a row only on approval.
    ask_before_backlog_sweep_closes: bool = setting(
        default=True,
        spec=SettingSpec(help="ask the operator before a backlog sweep closes an issue", parse=_parse_strict_bool),
    )


@dataclass
class _DirectiveLoopSettings:
    """The directive intake arc's per-tick budget and its verification horizon."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "directive_loop")

    # #3649 — bounds only the INERT pre-admission arc (interpret → clarify → ratify-ask →
    # admit), which writes nothing and terminates at the human ratify gate, so a backlog
    # reaches the owner in a bounded number of ticks. Execution stays one per tick.
    directive_intake_per_tick: int = setting(
        25,
        spec=SettingSpec(help="how many owner directives the directive loop intakes per tick", parse=_parse_strict_int),
    )
    # North-star PR-7 — how long after a ratified activation the five evidence classes are
    # judged.
    directive_verify_days: int = setting(
        7,
        spec=SettingSpec(
            help="days a ratified directive is watched before it is considered settled", parse=_parse_strict_int
        ),
    )


@dataclass
class _DogfoodLoopSettings:
    """The provision-smoke pass — which overlay it exercises."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "dogfood")

    # Which overlay anchor the placeholder task is created against — empty falls back to
    # the active overlay resolved via ``discover_active_overlay``.
    dogfood_smoke_overlay: str = setting(
        "", spec=SettingSpec(help="overlay the dogfood smoke run exercises", parse=_parse_strict_str)
    )


@dataclass
class _DreamLoopSettings:
    """The nightly dream pass: which phases run, and how much one pass may spend.

    Each phase is a declared setting rather than a sub-key of the ``loops`` container,
    because ``loops`` is ALSO the loop SEED table: ``[loops.dream]`` in ``defaults.toml``
    is the dream ``Loop`` row's own definition, so one key carried two schemas and a
    stored seed table answered every phase lookup with ``None``.
    """

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "dream")

    # Dream's derivation and live validation run on every admitted pass; the
    # pass budget bounds their metered work.

    # A pass batches its memory promotions into ONE ticket, so this ships ON.
    dream_memory_promote: bool = setting(
        default=True,
        spec=SettingSpec(help="let the dream pass promote a core-gap memory to a fix", parse=_parse_strict_bool),
    )


@dataclass
class _FollowupLoopSettings:
    """Review nag timing and mention policy for the followup loop."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "followup")

    review_nag_reask_mention: str = setting(
        "",
        spec=SettingSpec(
            help=("user group id or handle, or user handle, used when no engaged reviewer maps to Slack"),
            parse=_parse_strict_str,
            category=Category.PERSONAL,
        ),
    )


@dataclass
class _HousekeepingLoopSettings:
    """The self-update pass — how often it fast-forwards, and what it does after."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "housekeeping")

    # #1249 Fast-forwards the editable teatree clone + every registered overlay clone to
    # ``origin/<default>`` once the cadence has elapsed. Hourly keeps the orchestrator
    # current without spamming the upstream remote on every tick.
    # ``auto_update_require_green_main`` fails closed on non-green default-branch CI.
    auto_update_require_green_main: bool = setting(
        default=True, spec=SettingSpec(help="only self-update onto a main whose CI is green", parse=_parse_strict_bool)
    )
    # The same fast-forward over each WORK repo's main clone under ``$T3_WORKSPACE_DIR``,
    # so a clone never drifts behind after a merge and poisons a ``git show`` / ``grep``
    # investigation. Hourly keeps the clones current without spamming each remote.
    pull_main_clone_cadence_hours: int = setting(
        1, spec=SettingSpec(help="hours between pulls of each main clone", parse=_parse_strict_int)
    )


@dataclass
class _InboxLoopSettings:
    """The GitLab group-webhook pull — which Pub/Sub subscription the inbox loop drains."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "inbox")

    # The relay's pull subscription as ``projects/<project>/subscriptions/<name>``; empty is OFF (no job built).
    gitlab_events_subscription: str = setting(
        "",
        spec=SettingSpec(
            help=("Pub/Sub subscription the inbox loop pulls GitLab webhook events from; empty is off"),
            parse=_parse_strict_str,
            box_global=True,
        ),
    )


@dataclass
class _IssueImplementerLoopSettings:
    """The intake scanner's repo scope, walk budget, and idle-hold window; ceilings and labels are kill switches."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "issue_implementer")

    issue_intake_repos: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help=(
                "Repo slugs replacing the overlay-owned intake scope; undeclared repos allowed; "
                "branch-like prefixes rejected; empty or all-invalid uses owned repos"
            ),
            parse=_parse_str_list,
        ),
    )

    # Below the scan phase's 60s pool deadline on purpose: past that one the thread is
    # abandoned rather than stopped, so it keeps mutating rows after the tick ended and
    # records no resume point for the next pass (#4466).
    issue_intake_pass_budget_seconds: float = setting(
        45.0,
        spec=SettingSpec(
            help="seconds the intake scanner walks candidates before recording a resume point",
            parse=_parse_strict_float,
        ),
    )
    issue_intake_idle_hold_hours: int = setting(
        0,
        spec=SettingSpec(
            help="hours a started ticket may sit idle before intake claims nothing new; 0 is off",
            parse=_parse_strict_int,
        ),
    )


@dataclass
class _NewsLoopSettings:
    """The news scan — how often it runs and whether it may file."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "news")

    scanning_news_cadence_hours: int = setting(
        24, spec=SettingSpec(help="hours between news scans", parse=_parse_strict_int, box_global=True)
    )
    # #1391 Default ON: the skill records a ``PendingArticleSuggestion`` per candidate and
    # files an issue only on approval, because backlog pollution from unconfirmed
    # auto-filing is the failure mode this gate forecloses.
    ask_before_creating_news_tickets: bool = setting(
        default=True,
        spec=SettingSpec(help="ask the operator before the news scanner files a ticket", parse=_parse_strict_bool),
    )

    # T4-PR-3 — the measurement horizon after an experiment merges, the experiments
    # admitted per rolling 7-day window, and the convergence brake: after this many
    # consecutive non-KEPT decisions the loop parks itself rather than propose a fourth.


@dataclass
class _ResourcePressureLoopSettings:
    """The auto-free pass: how often it measures, and the free-byte bands it acts on."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "resource_pressure")

    # Measures ABSOLUTE reclaimable bytes (``vm_stat`` pages) — never
    # percent-of-nominal, which macOS "99 % RAM used" misleads about.
    ram_warn_avail_gb: float = setting(
        3.0, spec=SettingSpec(help="available RAM below this many GB warns", parse=_parse_strict_float)
    )
    ram_crit_avail_gb: float = setting(
        1.5, spec=SettingSpec(help="available RAM below this many GB is critical", parse=_parse_strict_float)
    )
    # Measured cost of one agent in full verification over the idle baseline. The number
    # that varies by box, hence a setting: it is what one admitted ticket is sized at.
    intake_ram_per_agent_gb: float = setting(
        6.2,
        spec=SettingSpec(
            help="RAM one admitted ticket is sized at when deriving intake concurrency", parse=_parse_strict_float
        ),
    )
    # Headroom held back rather than admitted against, so a burst is absorbed instead of
    # becoming an OOM — and so tightening lowers the limit BEFORE the memory ceiling.
    intake_ram_reserve_gb: float = setting(
        4.0,
        spec=SettingSpec(
            help="RAM held back from intake sizing so a burst is absorbed instead of OOMing", parse=_parse_strict_float
        ),
    )


@dataclass
class _ReviewLoopSettings:
    """Which authors' pull requests the review loop admits to the board."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "review")

    # #3569 Self-authored open PRs are ALWAYS admitted. COLLEAGUE / requested-reviewer PRs
    # are admitted only when this is true: the intake builds the ``ReviewerPrsScanner``
    # only when set, so false stops colleague PRs reaching the board while self-review
    # still runs. The author distinction lives HERE, upstream — the review execution is
    # blind to author.
    admit_colleague_prs_to_board: bool = setting(
        default=True,
        spec=SettingSpec(help="put pull requests opened by colleagues onto the review board", parse=_parse_strict_bool),
    )


@dataclass
class _SelfImproveLoopSettings:
    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "self_improve")

    self_improve_owner_wait_minutes: int = setting(
        60,
        spec=SettingSpec(
            help="minutes before one still-open repair notice", parse=parse_self_improve_owner_wait_minutes
        ),
    )


@dataclass
class _SnapshotWarmerLoopSettings:
    """When a reference-DB snapshot counts as stale enough to re-warm."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "snapshot_warmer")

    # Refreshed out-of-band so a ticket-critical-path provision never has to.
    snapshot_warmer_max_age_days: int = setting(
        1, spec=SettingSpec(help="days a warmed snapshot stays usable", parse=_parse_strict_int, box_global=True)
    )


@dataclass
class _TicketsLoopSettings:
    """The task sweep — off-switch and anti-thrash window."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "tickets")

    # #129 Verifies open teatree Task rows against their artifact's terminal state (issue
    # closed / PR merged) and completes only on durable proof, never in bulk and never on
    # a stale read.
    # The per-task window a swept task is skipped within, and the idempotency window for
    # the atomic ``last_sweep_check_ts`` stamp.
    task_sweep_recheck_interval_hours: int = setting(
        1, spec=SettingSpec(help="hours between rechecks of a swept task", parse=_parse_strict_int)
    )


#: Every per-loop base, in the order its group renders under ``Loops``.
LOOP_OWNED_SETTING_BASES: tuple[type, ...] = (
    _ArchReviewLoopSettings,
    _BacklogSweepLoopSettings,
    _DirectiveLoopSettings,
    _DogfoodLoopSettings,
    _DreamLoopSettings,
    _FollowupLoopSettings,
    _HousekeepingLoopSettings,
    _InboxLoopSettings,
    _IssueImplementerLoopSettings,
    _NewsLoopSettings,
    _ResourcePressureLoopSettings,
    _ReviewLoopSettings,
    _SelfImproveLoopSettings,
    _SnapshotWarmerLoopSettings,
    _TicketsLoopSettings,
)
