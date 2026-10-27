"""TeaTree config dataclasses — ``UserSettings``, ``TeaTreeConfig``, ``OverlayEntry``.

The flat persisted schema and its field defaults. WHERE a value may come from and
HOW a stored one is coerced live in the sibling ``setting_registries``. Both are
re-exported from ``teatree.config`` so every ``teatree.config.<name>`` path stays
valid.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from teatree.config.agent_enums import AgentHarness, AgentHarnessProvider, parse_harness_name
from teatree.config.declaration import Category, SettingSpec, setting
from teatree.config.enums import Autonomy, Mode, PrReviewBackend, Wip
from teatree.config.mr_reminder import MrReminderConfig, parse_mr_reminder_setting
from teatree.config.setting_parsers import (
    _parse_str_list,
    _parse_strict_bool,
    _parse_strict_float,
    _parse_strict_int,
    _parse_strict_str,
    _parse_user_identity_aliases,
)
from teatree.config.settings_agent import PYDANTIC_AI_MAX_TOKENS_DEFAULT, _ModeHarnessSettings
from teatree.config.settings_communication import _OnBehalfSettings
from teatree.config.settings_loop_flags import _LoopFlagAndCredentialSettings
from teatree.config.settings_loop_owned import (
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
from teatree.config.settings_provisioning import (
    WRITE_CONCURRENCY_PER_CORE,
    WRITE_CONCURRENCY_PER_CORE_MAX,
    WRITE_CONCURRENCY_PER_CORE_MIN,
    _ProvisioningSettings,
)
from teatree.config.speak import parse_speak_setting
from teatree.types import DEFAULT_MR_TITLE_REGEX, SpeakConfig

__all__ = [
    "PYDANTIC_AI_MAX_TOKENS_DEFAULT",
    "WRITE_CONCURRENCY_PER_CORE",
    "WRITE_CONCURRENCY_PER_CORE_MAX",
    "WRITE_CONCURRENCY_PER_CORE_MIN",
    "AgentHarness",
    "AgentHarnessProvider",
    "Autonomy",
    "Mode",
    "OverlayEntry",
    "TeaTreeConfig",
    "UserSettings",
    "parse_harness_name",
]


@dataclass
class OverlayEntry:
    name: str
    overlay_class: str
    project_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def canonical_overlay_name(name: str) -> str:
        """The route/dedup key for an overlay identifier: ``name`` minus the ``t3-`` prefix.

        A TOML overlay table and the ``t3-``-prefixed entry point that registers
        it address the same overlay; this strip is the key under which CLI
        sub-apps are routed and deduplicated so the pair cannot register two
        sub-apps.

        This is the CLI-routing key only; stored overlay names use the full
        entry-point name.
        """
        return name.removeprefix("t3-")


@dataclass
class _WorkspaceCoreSettings:
    """Workspace root + the core engagement / identity flags.

    A private in-file group base (see :class:`UserSettings`). Pure data-declaration —
    no behaviour — so the flat persisted schema is preserved by inheritance.

    ``GROUP_PATH`` is where this base's fields render in the settings hierarchy —
    a ``ClassVar``, so it is declaration metadata and never a persisted field. It is
    the ONLY place this group is named: ``teatree.config.setting_groups`` reads it off
    the MRO, so a field added below is placed by it with nothing else to edit.
    """

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Workspace", "Engagement & identity")

    workspace_dir: Path = setting(
        default_factory=lambda: Path.home() / "workspace",
        spec=SettingSpec(
            help="directory new worktrees are created under",
            parse=_parse_strict_str,
            category=Category.PERSONAL,
            stored=str,
            storage_default="",
        ),
    )
    # #256 Default-OFF teatree engagement. When false (the default) a fresh
    # Claude session does NOT auto-engage teatree — no skill auto-suggest, no
    # PreToolUse load-block, no loop scheduling — and SessionStart shows a
    # one-line how-to-start advisory instead. The owner flips it true to
    # auto-activate every session. DB-home (DB-home cutover): the cold
    # SessionStart hook and the PreToolUse gates read it DB-ONLY pre-Django via the
    # Django-free ``cold_reader`` (``teatree_settings._cold_db_bool``) and the bash
    # ``statusline.sh._autoload_db_value`` (sqlite3 CLI); ``T3_AUTOLOAD`` env wins, a
    # ``[teatree] autoload`` TOML value is ignored on read. Explicitly calling
    # ``/teatree`` — or loading any ``t3:`` skill — engages teatree for the
    # session regardless of this default.
    autoload: bool = setting(
        default=False,
        spec=SettingSpec(help="load the suggested skills automatically at session start", parse=_parse_strict_bool),
    )
    contribute: bool = setting(
        default=False, spec=SettingSpec(help="allow a retro to edit teatree's own skills", parse=_parse_strict_bool)
    )
    excluded_skills: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(help="skills never offered to an agent, whatever the loader suggests", parse=_parse_str_list),
    )


@dataclass
class _LoopSettings:
    """WIP dial + loop cadence/runner + the worker admission gate."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "Cadence & throughput")

    # How much new work a loop tick admits at once — the bounded-WIP dial.
    # ``slow`` caps to one impl worker; ``medium`` is the conservative baseline
    # (NO orchestrator fan-out — only the intrinsic loop + PR sweep + per-overlay
    # ``max_concurrent_auto_starts`` provide throughput); ``full`` (the default)
    # arms the /t3:wip loop; ``boost`` keeps ``boost_concurrency = N`` workers
    # live, refilling the pool each tick as workers exit. Orthogonal
    # to ``mode``/``autonomy`` (those gate *whether* a publish proceeds; this
    # governs *how many* threads run) and never relaxes a safety gate.
    # Per-overlay overridable; ``T3_WIP`` env wins over both.
    wip: Wip = setting(Wip.FULL, spec=SettingSpec(help="how much work may be in progress at once", parse=Wip.parse))
    # #3634 The wip dial's PHASE SPLIT, replacing the single blunt concurrency knob.
    # ``write_wip`` is how many implementation workers (coding / testing / reviewing)
    # run concurrently; ``merge_wip`` is the merge lane, single-flight by design so
    # the next PR always rebases against what just landed. A ``merge_wip`` above 1
    # forfeits that conflict-safety guarantee, so the resolver clamps it back to 1.
    # DB-home, per-overlay overridable; ``T3_WRITE_WIP`` / ``T3_MERGE_WIP`` env win.
    write_wip: int = setting(
        3, spec=SettingSpec(help="how many writing units may be in flight at once", parse=_parse_strict_int)
    )
    merge_wip: int = setting(
        1, spec=SettingSpec(help="how many merges may be in flight at once", parse=_parse_strict_int)
    )
    # Loop tick interval in seconds (BLUEPRINT § 5.6). Default 12 minutes.
    loop_cadence_seconds: int = setting(
        720,
        spec=SettingSpec(
            help="default seconds between ticks of a loop that declares none", parse=_parse_strict_int, box_global=True
        ),
    )
    # The drain-then-deploy admission gate (rolling/zero-downtime deploy). Default
    # OFF: the worker admits new work normally. `t3 worker drain` flips it ON for the
    # deploy window so the claim/admission path admits ZERO new tasks — the CAS
    # `claim_next_pending` and the `claimable()` query both short-circuit —
    # while in-flight CLAIMED leases keep renewing and finish. It is READ only at the
    # claim chokepoint; it deliberately does NOT feed the worker supervisor's
    # zero-admitted stop condition, so quiescing never stops the supervisor or kills a
    # live sub-agent. The FRESH worker's init clears it so admission resumes.
    # DB-home (#1775): resolved from the `ConfigSetting` store (global + overlay rows)
    # + `T3_WORKER_QUIESCING` env; a TOML value is ignored on read. Set via `t3 worker
    # drain` (which writes it) or `config_setting set worker_quiescing`.
    worker_quiescing: bool = setting(
        default=False,
        spec=SettingSpec(
            help=(
                "deploy drain gate — cleared on a fresh init; freeze the factory with `t3 loop preset use off --hold`"
            ),
            parse=_parse_strict_bool,
        ),
    )


@dataclass
class _IdentityRoutingSettings:
    """Statusline chain, operator identity aliases, repo mode, and the missing-issue policy."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Communication", "Identity & routing")

    statusline_chain: list[str] = setting(
        default_factory=list, spec=SettingSpec(help="which statusline segments render, in order", parse=_parse_str_list)
    )
    # Usernames / handles that all map to the same human operator across
    # platforms (a GitHub login, a GitLab username, an internal handle).
    # Two consumers:
    # - The ticket-disposition scanner uses them to suppress the reassign
    #   signal when an issue is handed off between two of the operator's
    #   own identities — plumbing noise, not an actionable transition
    #   (souliane/teatree#975).
    # - The loop's PR/MR scanners union-query each alias so cross-forge
    #   work (e.g. multiple GitHub logins under one PAT, GitHub vs GitLab
    #   handles for the same human) surfaces in the statusline (#976).
    # Default empty preserves legacy single-identity behaviour. Per-overlay
    # overridable so a tracker-scoped overlay can carry tracker-specific
    # handles without flipping the global default.
    user_identity_aliases: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help="other identities that are the same person as the operator",
            parse=_parse_user_identity_aliases,
            category=Category.PERSONAL,
        ),
    )
    # Solo vs collaborative working mode (issue #550 item 4). Empty string
    # = auto-detect from `git shortlog` history (see teatree.repo_mode);
    # an explicit "solo" / "collaborative" pins the verdict and bypasses
    # detection. Consumed by skills via `t3 tool repo-mode` so the
    # ask-first-vs-fix-proactively decision lives in one place, not in
    # every skill.
    repo_mode: str = setting(
        "",
        spec=SettingSpec(
            help="whether this box works on its own repos, colleagues' repos, or both",
            parse=_parse_strict_str,
            stored=Literal["", "solo", "collaborative"],
        ),
    )


@dataclass
class _ArchitecturalReviewSettings:
    """The skill the periodic architectural review dispatches — its cadence is the loop's."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Architectural review")

    # #1136 / #1152 Periodic architectural-review scanner — CORE always-on (not
    # per-overlay opt-in). The cadence applies uniformly to every overlay's
    # worktrees because it is a teatree-platform behaviour; the ``arch_review``
    # Loop row (and any preset masking it) is what turns the scanner off.
    architectural_review_skill: str = setting(
        "architectural-review",
        spec=SettingSpec(help="skill the architectural review loop runs", parse=_parse_strict_str),
    )


@dataclass
class _ReviewGateSettings:
    """Review-phase evidence gates + review-board admission + the E2E confidence bar."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Review")

    # Identities this deployment additionally trusts as INDEPENDENT checkers (#4241),
    # on top of the harness's recognised reviewer-role tokens. maker≠checker is a
    # fail-CLOSED allowlist, so a reviewer whose handle carries no role word — every
    # human, an external review service, a novel agent role — is refused until it is
    # named here (the owner's own `user_identity_aliases` are unioned in for free, so
    # the common case needs no configuration). Widening a fail-closed trust boundary is
    # itself an authorization, so the key is a `SAFETY_POSTURE_KEYS` member the MCP
    # write surface refuses. DB-home (#1775), per-overlay overridable.
    independent_reviewer_identities: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help=("extra identities trusted to record an independent review, beside the operator's own handles"),
            parse=_parse_str_list,
        ),
    )

    # #1539 Per-ticket deep-review skill. Empty = opt-in unset: the
    # reviewing-phase evidence gate (``teatree.core.gates.review_skill_gate``) is
    # a NO-OP, so projects that do not configure a review skill keep
    # recording the ``reviewing`` attestation unchanged. When set (e.g.
    # ``ac-reviewing-codebase``), ``lifecycle visit-phase <id> reviewing``
    # refuses to record the phase without durable evidence the skill ran.
    # Distinct from ``architectural_review_skill`` (the periodic cadence
    # scanner) — this one gates a single ticket's reviewing attestation.
    review_skill: str = setting("", spec=SettingSpec(help="skill a review dispatch runs", parse=_parse_strict_str))
    # Additional skills whose recorded run ALSO satisfies the reviewing-phase
    # evidence gate — substitutes for ``review_skill``, never a bypass. An
    # overlay that accepts a second deep reviewer (a different model's, say)
    # names it here so either recorded run passes while a run of NEITHER stays
    # refused. Empty default = the single-skill behaviour; alternates alone never
    # arm the gate, which stays keyed on ``review_skill`` being set.
    review_skill_alternates: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help="extra skills whose recorded run also satisfies the reviewing-phase evidence gate",
            parse=_parse_str_list,
        ),
    )
    # How long a review backend stays parked after its account ran out of quota
    # (``ReviewBackendCooldown``). Long enough that `auto` stops re-probing every
    # tick — each probe costs a burned dispatch to rediscover the same exhaustion —
    # and short enough that a refilled window is picked up the same working day.
    review_backend_cooldown_hours: int = setting(
        6,
        spec=SettingSpec(
            help="hours a review backend stays parked after its account ran out of quota", parse=_parse_strict_int
        ),
    )
    # Which reviewer executes the self-authored-PR cold review. ``auto`` (the
    # default) resolves per tick — codex when its binary is present and not
    # cooling down from a quota exhaustion, else claude — so a codex-less box or
    # an exhausted account keeps getting reviews. An explicit ``claude`` /
    # ``codex`` pin is honoured as written and never silently degrades.
    pr_review_backend: PrReviewBackend = setting(
        PrReviewBackend.AUTO,
        spec=SettingSpec(
            help="which reviewer runs the self-authored-PR cold review; auto resolves per tick",
            parse=PrReviewBackend.parse,
        ),
    )


@dataclass
class _MergeGateSettings:
    """The gates a ticket must clear to reach MERGED — evidence, plan, repro, debt."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Merge & done")

    forced_repro_gate_enabled: bool = setting(
        default=True,
        spec=SettingSpec(
            help=(
                "require an executed pre-fix failing run for fix tickets at coding, testing and ship; "
                "off also stops seeding the rubric criteria that demand one and makes observed_red optional"
            ),
            parse=_parse_strict_bool,
        ),
    )

    # The branch-protection required-status-check contexts the operator KNOWS must
    # gate a merge on this overlay's repos (e.g. ``["test (3.13)"]``). A fail-closed
    # floor: when the forge reports a DETERMINATE-EMPTY required set (branch
    # protection removed or never configured) while this floor is non-empty, the
    # keystone CI verdict fails closed to ``failed`` — a removed branch-protection
    # gate can no longer classify as "all checks passed / green". Default empty =
    # NO-OP (a genuinely gate-less repo still merges); the operator opts in per
    # overlay once their repos carry required checks. Per-overlay overridable.
    expected_required_contexts: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help=(
                "CI check names a pull request must carry before it can merge (workflow "
                "names on a plan-restricted GitHub Free fallback — #4844)"
            ),
            parse=_parse_str_list,
        ),
    )


@dataclass
class _CriticGateSettings:
    """The user-proxy critic's posture, the send-proxy destination guard, the bulk-close cap."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Critic & send proxy")

    # #117 send-proxy — every outbound artifact (Slack post/DM/react, forge
    # PR/MR/issue comment) routes through ``teatree.core.send_proxy``, which
    # redaction-scans the payload and checks the destination against
    # ``send_proxy_allowlist``. It refuses a non-allowlisted destination and
    # redacts matching terms from the payload.
    # The per-overlay destination allowlist: ``fnmatch`` globs over the raw
    # destination (Slack channel id or ``org/repo`` slug) and the
    # channel-qualified ``<channel>:<destination>`` form.
    # Empty by default. The user's own DM is always allowed (never-lockout carve-out),
    # so an empty allowlist can never gate the bot→user notify path. Per-overlay overridable.
    send_proxy_allowlist: list[str] = setting(
        default_factory=list, spec=SettingSpec(help="destinations the send proxy may deliver to", parse=_parse_str_list)
    )
    # PR-08 No-bulk-close threshold: a single command/agent action closing more
    # than this many tickets/MRs is refused without an explicit per-item
    # confirmation token (``bulk_close_gate``). A close of ≤ threshold items is
    # always allowed. Per-overlay overridable.
    bulk_close_threshold: int = setting(
        5,
        spec=SettingSpec(
            help="how many closes in one run count as a bulk action needing approval", parse=_parse_strict_int
        ),
    )


@dataclass
class _ScannerSettings:
    """What the loop fan-out sweeps, and whether it may start at all — box facts, not one loop's."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "Fan-out & admission")

    #: Which overlays the full-fleet scanners sweep; empty sweeps every registered overlay.
    #: A property of the BOX, not of whichever preset is active — it answers "whose repos
    #: does this factory look after", and that does not change when the operator goes AFK.
    scanner_overlay_scope: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help="overlays the full-fleet scanners sweep; empty sweeps every registered overlay", parse=_parse_str_list
        ),
    )
    schema_readiness_gate_enabled: bool = setting(
        default=True,
        spec=SettingSpec(
            help="refuse new work while the control database schema appears behind the code", parse=_parse_strict_bool
        ),
    )


# The regenerable cache dirs auto-purged at CRITICAL disk pressure (the
# ``disk_cache_allowlist`` default). A module constant so the field default stays a
# single line; ``.copy`` gives each settings instance its own list.
#
# ``~/.cache/prek`` and ``~/.cache/uv`` are deliberately NOT here: prek's per-hook
# environments have unknown rebuild cost (opt in explicitly), and uv's cache is
# already pruned safely by the freeing ladder's own ``uv cache prune`` step. An
# entry that names nothing on this host is reported ABSENT in the plan rather
# than counted as a 0.00 GB purge (#3852).
_DEFAULT_DISK_CACHE_ALLOWLIST = [
    "~/.cache/pre-commit",
    "~/.cache/puppeteer",
    "~/.cache/codex-runtimes",
    "~/.cache/go-build",
]


@dataclass
class _ResourcePressureSettings:
    """The free-disk bands auto-free acts on, what it may reclaim, and the intake arm it drives."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Resource pressure", "Thresholds & cadence")

    # #3992 The resource loop derives issue-intake concurrency from observed headroom
    # instead of it being a hand-set constant. Flipping this OFF is the kill-switch:
    # ``issue_implementer_max_concurrent`` is then used verbatim, as before.
    # Allow-LIST only (never a denylist): exactly these regenerable cache dirs
    # are auto-purged at CRITICAL. ``uv`` is handled via ``uv cache prune``.
    # ``~/.cache/prek`` and ``~/.claude/projects`` are deliberately absent —
    # the latter is hard-protected even if a user adds it.
    disk_cache_allowlist: list[str] = setting(
        default_factory=_DEFAULT_DISK_CACHE_ALLOWLIST.copy,
        spec=SettingSpec(help="cache directories disk reclamation may empty", parse=_parse_str_list),
    )
    # Measures ABSOLUTE free bytes (``os.statvfs``) — never percent-of-nominal, which
    # the APFS shared-container total misleads about.
    disk_crit_free_gb: float = setting(
        10.0, spec=SettingSpec(help="free disk below this many GB is critical", parse=_parse_strict_float)
    )
    disk_warn_free_gb: float = setting(
        25.0, spec=SettingSpec(help="free disk below this many GB warns", parse=_parse_strict_float)
    )
    # #4244 The retention policy for the checkout pool: a ``.venv`` untouched for
    # this long is evicted as the pure cache it is (``uv sync`` rebuilds it), so
    # the pool's steady state is roughly one venv per checkout worked inside the
    # window rather than one per checkout ever created. Not a destructive lever —
    # a build product holds no work — but a live process inside a checkout always wins.
    artifact_idle_days: float = setting(
        2.0,
        spec=SettingSpec(
            help=(
                "days a checkout's rebuildable build products may sit untouched before "
                "eviction — a ceiling that decays with free disk between "
                "disk_warn_free_gb and disk_crit_free_gb, and stops gating below it"
            ),
            parse=_parse_strict_float,
        ),
    )
    # #4580 A process group whose leader is gone is reported once it has run this long
    # and is still burning. The only knob: the age is what an operator retunes when the
    # finding nags, while the burn-rate floor and the never-reap protect-list stay in code
    # — an operator-emptiable safety list is a footgun with no upside.
    orphan_group_min_age_hours: int = setting(
        6,
        spec=SettingSpec(
            help="hours a leaderless process group must have run before it is reported", parse=_parse_strict_int
        ),
    )


@dataclass
class _RetentionSettings:
    """Task-sweep, stack concurrency, control-DB retention windows, and session staleness."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Retention & sweeps")

    # Read by the dream pass, the backlog-sweep scanner and retro, so it belongs to no one loop.
    dream_umbrella_url: str = setting(
        "https://github.com/souliane/teatree/issues/2663",
        spec=SettingSpec(
            help="issue whose ticket queues the dream pass's collected gaps for the backlog sweep",
            parse=_parse_strict_str,
        ),
    )

    # The branch every shipped PR targets when the ticket does not name one
    # itself. Empty (the default) keeps the historical behaviour — the repo's
    # own default branch. Set it when a whole line of work stacks onto ONE
    # long-lived integration branch instead of ``main``: every PR then targets
    # that branch, and ``run_branch_currency_gate`` merges it into each
    # worktree before shipping, so a merge into the integration branch
    # propagates to the branches still in flight. The repo-keyed
    # ``Ticket.extra['target_branch'][repo_slug]`` override still wins, and a
    # branch that IS the configured target falls back to the repo default so
    # the integration branch itself never targets itself.
    target_branch: str = setting(
        "",
        spec=SettingSpec(
            help=("long-lived integration branch every pull request targets instead of the repo default"),
            parse=_parse_strict_str,
        ),
    )
    # #1397 Cap on concurrent locally-running stacks for a single overlay.
    # Each running worktree (``services_up``/``ready``) holds docker
    # containers, browsers, language servers, and CI processes — on a
    # memory-constrained host (one OOM observed 2026-05-27 when two stacks
    # ran in parallel), one stack at a time is the workable limit. The
    # ``t3 <overlay> worktree start`` / ``workspace start`` gate refuses to
    # advance a second stack into ``SERVICES_UP`` while another is already
    # there, naming the blockers and pointing at ``worktree teardown``.
    # Default ``1``: for unattended 24/7 headless operation a single in-flight
    # local stack avoids merge conflicts against ``main`` and conserves the
    # Anthropic 5h token window. Set ``0`` to restore the legacy unbounded
    # behaviour, or any higher positive integer to raise the cap. Per-overlay
    # overridable: a heavy overlay can cap to ``1`` while a cheap dogfood
    # overlay stays unbounded (``0``).
    max_concurrent_local_stacks: int = setting(
        1, spec=SettingSpec(help="how many tickets may hold a running local stack at once", parse=_parse_strict_int)
    )
    # #3693 retention window for the high-churn ``TaskAttempt`` table. A ``prune``
    # deletes rows OLDER than the window whose owning ticket/task is TERMINAL —
    # never a live/in-flight row, and never within the window. Days, not a byte
    # ceiling: age is the operator-legible, safe-by-construction lever. It
    # defaults to a conservative 30 so a fresh install never prunes recent history;
    # ``0`` disables that table's pruning entirely. Per-overlay overridable.
    task_attempt_retention_days: int = setting(
        30,
        spec=SettingSpec(
            help=(
                "days a quiet finished ticket's task history is kept, never below 56; 0 "
                "disables, and the hourly pass reads it for the active overlay like any "
                "other setting"
            ),
            parse=_parse_strict_int,
        ),
    )
    # #3871 kill switch for the FSM-audit-trail lane. There is no window: the lane's
    # trigger is the ticket CLOSING, not a row aging, and what it removes is decided
    # per row — a ``from_state == to_state`` row records no edge, so it is not history
    # and a reopened ticket does not need it. Every real state edge is kept for as
    # long as the ticket exists (~410 rows on the measured box), so no age bound
    # applies to them. Per-overlay overridable.
    # #3871 window for ``django_tasks_db``' own ``DBTaskResult`` table. The delete is
    # the library's shipped ``prune_db_task_results`` command; this is only how far
    # back it is told to go. Short because nothing in teatree reads a FINISHED
    # result row — every consumer filters on READY or RUNNING — so a finished row is
    # pure post-mortem material, and the table takes ~400k of them a day. 1 day
    # matches the cadence the maintenance chain already ran at. ``0`` disables the
    # lane (the library's own floor is 1 day; sub-day retention is not expressible).
    task_result_retention_days: int = setting(
        1, spec=SettingSpec(help="days a task result row is kept", parse=_parse_strict_int)
    )
    # #4165 agent-scratch retention under the temp root. On this box ``/tmp`` is a
    # RAM-backed tmpfs, so week-old sqlite/venv scratch is not idle disk — it is
    # 28% of the working memory pool, permanently. Ships OFF (``0``) because the
    # lane is a recursive delete and every destructive lever defaults OFF; 3 days
    # is the window the manual reclaim used, and arming it needs a venue whose
    # liveness probe can actually resolve host fds. The root is blank by default
    # and auto-resolves to the host view (``/host-tmp``, paired with
    # ``/host-proc``) when the deployment mounts it, else this venue's own
    # ``/tmp`` — a containerised sweep of the container's own temp root reclaims
    # nothing of what actually fills the box. Per-overlay overridable.
    scratch_retention_days: int = setting(
        0,
        spec=SettingSpec(
            help=("days of agent scratch the sweep reclaims under the temp root; disabled by default"),
            parse=_parse_strict_int,
        ),
    )
    scratch_sweep_root: str = setting(
        "",
        spec=SettingSpec(
            help="temp root the scratch sweep reclaims from (blank auto-resolves the host view)",
            parse=_parse_strict_str,
        ),
    )
    # Fail-safe staleness bound on the OPEN-Session liveness signal every reaper
    # consults (``Ticket.has_active_work``). An agent that crashed without closing
    # its Session would otherwise pin its ticket — and so its worktree, its
    # ``max_concurrent_local_stacks`` slot, and ``workspace relocate`` — busy
    # forever. An open Session whose last recorded activity (its own
    # ``started_at``, its tasks' heartbeats, its attempts' start times) is older
    # than this many hours is NOT live. 12h is 4x the ``watchdog_max_runtime_seconds``
    # hard cap on a single agent run, so it cannot mask real in-flight work — and
    # an active (PENDING/CLAIMED) task keeps a ticket busy with NO time bound at
    # all, independently of this. ``0`` disables the bound (every open Session is
    # live). Per-overlay overridable.
    session_stale_after_hours: int = setting(
        12,
        spec=SettingSpec(help="hours after which a session with no heartbeat counts as stale", parse=_parse_strict_int),
    )


@dataclass
class _PrePublishGateSettings:
    """Slack voice, speak/mr-reminder, the pre-publish gate kill-switches, repo patterns, review batching."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Pre-publish")

    # #2060 The resolved speak config — a local playback enum (off/dm/all) + a
    # slack bool. DB-home (#1775, DB-home cutover): stored as a JSON dict
    # ConfigSetting (``parse_speak_setting``), rebuilt bespoke by the resolver; the
    # cold Stop hook reads it via ``cold_reader``. See :class:`SpeakConfig`.
    speak: SpeakConfig = setting(
        default_factory=SpeakConfig,
        spec=SettingSpec(
            help="text-to-speech delivery, and which surfaces are spoken",
            parse=parse_speak_setting,
            stored=dict[str, Any],
        ),
    )
    # The resolved slug→channel routing table for the cross-repo "my open MRs"
    # reminder; empty default keeps it inert. DB-home (#1775): stored as a JSON dict
    # ConfigSetting (``parse_mr_reminder_setting``), rebuilt bespoke by the resolver.
    mr_reminder: MrReminderConfig = setting(
        default_factory=MrReminderConfig,
        spec=SettingSpec(
            help="when and how often a stale review request is chased",
            parse=parse_mr_reminder_setting,
            stored=dict[str, Any],
        ),
    )
    # #1398 Pre-publish close-trailer scanner. fnmatch patterns over
    # ``namespace/repo``: when an MR/PR target repo matches one of these
    # patterns and the body carries a ``Closes|Fixes|Resolves`` trailer,
    # the trailer line is silently stripped before publishing. Default
    # empty preserves legacy behaviour. DB-home (#1775); set via
    # ``t3 <overlay> config_setting set ban_close_trailers_on_namespaces``;
    # the TOML value is ignored on read.
    ban_close_trailers_on_namespaces: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(help="repo namespaces where a commit may not carry a closing trailer", parse=_parse_str_list),
    )
    # Repo patterns whose merge requests need no review request: the user asks for
    # review in person there, so a posted request is noise a colleague has to dismiss.
    # Matched by ``teatree.core.review.repo_exemption`` on the same host-stripped
    # leading-segment-prefix grammar ``slug_namespace_matches`` uses. This is the PIN layer over
    # the overlay's derived ``review_exempt_repo_slugs()`` and it wins in BOTH
    # directions — an entry ADDS an exemption, a ``!``-prefixed one SUBTRACTS one the
    # overlay declared, so a changed policy is a config edit rather than a merge. The
    # deepest matching pattern decides and a tie does not exempt. Empty default =
    # INERT (no repo is exempt). Per-overlay overridable (DB-home).
    review_exempt_repos: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help="repos whose merge requests need no review request (a `!` prefix un-exempts one)",
            parse=_parse_str_list,
        ),
    )
    # Whether a review-EXEMPT merge request still gates its work group's readiness.
    # ``True`` is the conservative reading — an exempt member keeps holding the group,
    # so the bias is toward NOT broadcasting a partial batch.
    review_exempt_repos_count_toward_group_readiness: bool = setting(
        default=True,
        spec=SettingSpec(help="let review-exempt merge requests still hold their work group", parse=_parse_strict_bool),
    )
    # Orchestrator-execution-boundary gate (#115, §17.6 gate 2). When
    # enabled (default), the main agent is blocked from running a HEAVY /
    # long-running foreground Bash command (test suite, build, dev
    # server, long sleep, full-tree sweep); ``run_in_background: true`` is
    # the escape hatch and sub-agents are unrestricted. The one-line
    # kill-switch ``[teatree] orchestrator_bash_gate_enabled = false``
    # disables the gate entirely (also read directly by the hook layer's
    # ``_orchestrator_bash_gate_enabled`` so a `t3 update` that reinstalls
    # the gate stays off until the user flips it back).
    orchestrator_bash_gate_enabled: bool = setting(
        default=True,
        spec=SettingSpec(help="refuse heavy shell commands run directly by the orchestrator", parse=_parse_strict_bool),
    )
    # Snapshot-baseline pre-commit gate (§17.6). When enabled (default), a
    # commit that stages a Playwright visual baseline (a file under
    # `__snapshots__/` / `<spec>-snapshots/`) is refused unless the ticket
    # carries a green + POSTED `E2eMandatoryRun`. Its OWN kill-switch —
    # `snapshot_baseline_gate_enabled` (per-overlay overridable, DB-first) —
    # disables the hook; the reader (`scripts/hooks/check_snapshot_baseline.py`)
    # resolves it through `get_effective_settings`, so a DB `config_setting set`
    # actuates it exactly like the other gates.
    snapshot_baseline_gate_enabled: bool = setting(
        default=True,
        spec=SettingSpec(help="refuse a snapshot diff taken against a stale baseline", parse=_parse_strict_bool),
    )
    # Anti-relaxation + tach-soundness pre-commit gate (§17.6.1/§17.6.2, #850).
    # When enabled (default), the `gate-relaxation` prek hook
    # (`scripts/hooks/check_gate_relaxation.py`) refuses a commit whose staged diff
    # relaxes a lint/coverage constraint or a tach boundary. Its OWN kill-switch —
    # `gate_relaxation_gate_enabled` (per-overlay overridable, DB-first) — disables
    # the hook; the reader resolves it through `get_effective_settings`, so a DB
    # `config_setting set` actuates it exactly like the sibling gates.
    gate_relaxation_gate_enabled: bool = setting(
        default=True,
        spec=SettingSpec(help="refuse a change that weakens a safety gate without approval", parse=_parse_strict_bool),
    )
    # #122 Safety-biased incremental push gate. SETTLING feature flag: ON (default)
    # => `dev/push-gate.sh` scopes the diff (doctest + ast-grep), FULL on every
    # uncertainty; OFF => whole-tree both sweeps (the pre-#122 behaviour). The
    # default flipped to ON after the CI `selection-audit` soak showed the scoped
    # selection never missed a whole-tree finding. The flag survives as a per-overlay
    # escape hatch; the CI whole-tree backstop is never removed regardless.
    colleague_repo_url_pattern: str = setting(
        "",
        spec=SettingSpec(
            help="regex matching repo URLs owned by colleagues rather than the operator", parse=_parse_strict_str
        ),
    )
    # Names THIS box in the dashboard header. Empty ships as the default because a
    # machine name cannot be a shipped constant; the header resolves empty to the
    # hostname, so a multi-machine operator can tell two dashboards apart unconfigured.
    dashboard_instance_label: str = setting(
        "",
        spec=SettingSpec(help="name shown in the dashboard header, to tell two boxes apart", parse=_parse_strict_str),
    )
    # The header mark, as a STATIC path rather than a filesystem one: a static path is
    # the same string on every box and in every checkout layout, and ``collectstatic``
    # already reaches every installed app's ``static/`` dir on each admin boot — so an
    # overlay ships its own logo beside its code and names it here. Promoted to an
    # overlay code default so that declaration lives in the overlay's repo; a path no
    # static finder resolves renders no mark rather than breaking the header.
    dashboard_logo: str = setting(
        "dash/logo.jpg",
        spec=SettingSpec(help="static path of the logo shown in the dashboard header", parse=_parse_strict_str),
    )
    solo_repo_url_pattern: str = setting(
        "", spec=SettingSpec(help="regex matching repos the operator works on alone", parse=_parse_strict_str)
    )
    # Conventional-Commits title pattern enforced at ``pr create`` BEFORE the
    # gh/glab network call (#1540). A non-matching title is rejected with the
    # pattern printed verbatim; the description is independently required to
    # carry a What/Why header. Per-overlay overridable via
    # ``[overlays.<name>].mr_title_regex = "…"`` so an overlay with a different
    # title grammar declares its own pattern without flipping the global.
    mr_title_regex: str = setting(
        DEFAULT_MR_TITLE_REGEX, spec=SettingSpec(help="regex a pull request title must match", parse=_parse_strict_str)
    )


@dataclass
class UserSettings(
    _WorkspaceCoreSettings,
    _ModeHarnessSettings,
    _LoopSettings,
    _OnBehalfSettings,
    _IdentityRoutingSettings,
    _ArchitecturalReviewSettings,
    _ReviewGateSettings,
    _MergeGateSettings,
    _CriticGateSettings,
    _ScannerSettings,
    _ResourcePressureSettings,
    _RetentionSettings,
    _ProvisioningSettings,
    _PrePublishGateSettings,
    _LoopFlagAndCredentialSettings,
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
):
    """The ``[teatree]`` settings — the FLAT, 160-field persisted contract.

    The fields are declared across the private in-file group bases above purely for
    readability; ``UserSettings`` is the sole public API and ``dataclasses.fields()``
    stays inheritance-transparent, so the flat field namespace (DB ``ConfigSetting.key``,
    env overrides, cold sqlite3 readers, the rename-guard and golden pin) is unchanged.

    Each base's ``GROUP_PATH`` is where its fields render in the settings hierarchy, and
    this bases tuple is the hierarchy's ORDER — ``teatree.config.setting_groups`` reads
    both off the MRO, so adding a base here is all it takes to add a group.

    CLAUDE.md's "composition over mixins" targets behaviour-carrying classes; these bases
    are pure data-declaration with no behaviour, and the flat schema IS the persisted
    contract, so grouping them as declaration bases (not composed attributes) is the
    deliberate divergence — nesting would be a ~160-key data migration, not a refactor.
    The ``tests/config/test_settings_group_partition.py`` guard pins the group field sets
    pairwise-disjoint and their union == ``dataclasses.fields(UserSettings)`` so a
    silently-shadowed duplicate field can never slip in.
    """


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)
