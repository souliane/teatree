"""TeaTree config dataclasses + the per-overlay / env override registries.

``UserSettings`` (the ``[teatree]`` table), ``TeaTreeConfig``, ``OverlayEntry``,
``E2ERepo``, the field ``_parse_*`` coercers, and the two override registries
(``OVERLAY_OVERRIDABLE_SETTINGS`` / ``ENV_SETTING_OVERRIDES``). Split out of the
package module for the module-health LOC cap; re-exported from
``teatree.config`` so every ``teatree.config.<name>`` path stays valid.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teatree.config.enums import Autonomy, Mode, OnBehalfPostMode, Speed
from teatree.config_mr_reminder import MrReminderConfig
from teatree.paths import DATA_DIR
from teatree.types import DEFAULT_MR_TITLE_REGEX, SlackVoiceClassifierMode, SpeakConfig


@dataclass
class E2ERepo:
    """An external git repository containing Playwright E2E tests."""

    name: str
    url: str
    branch: str
    e2e_dir: str = "e2e"


def _parse_excluded_skills(raw: object) -> list[str]:
    return [str(s) for s in raw] if isinstance(raw, list) else []


_DEFAULT_DISK_CACHE_ALLOWLIST = ("~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes")


def _parse_disk_cache_allowlist(raw: object) -> list[str]:
    """Coerce the disk cache allow-list, falling back to the regenerable-cache default.

    A missing key (``None``) yields the curated default set of regenerable
    caches; an explicit list (even empty) is honoured verbatim so a user can
    narrow the allow-list to nothing. Non-list scalars degrade to the default
    rather than raising.
    """
    if raw is None:
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    if not isinstance(raw, list):
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    return [str(s) for s in raw]


def _parse_env_bool(raw: str) -> bool:
    """Coerce a ``T3_*`` env-var string to a bool for ``ENV_SETTING_OVERRIDES``.

    Conservative truthy set (``1``/``true``/``yes``/``on``, case-insensitive);
    everything else — including ``false``/``0``/``no`` — resolves to ``False``.
    A kill-switch env var is meant to *disable*, so any value that is not an
    explicit enable reads as off.
    """
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_user_identity_aliases(raw: object) -> list[str]:
    """Coerce a TOML list of usernames/handles to ``list[str]``.

    Returns a deduped list of non-empty alias handles, in insertion order.
    Non-list inputs (a stray scalar) degrade to an empty list rather than
    raising — keeps the loader robust to a malformed override while leaving
    the suppression off by default. Consumed by the ticket-disposition
    scanner (#975) to suppress reassign signals between the operator's own
    identities, and by the loop's PR/MR scanners (#976) to union-query each
    alias so cross-forge work surfaces in the statusline.
    """
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(s) for s in raw if isinstance(s, str) and s))


# Registry of UserSettings fields that can be overridden per-overlay in
# ``[overlays.<name>]``. To make another setting overridable, add an entry
# here with a parser that coerces the raw toml value to the UserSettings
# field type. The getter `get_effective_settings()` applies overrides
# generically via ``dataclasses.replace`` — no per-setting wiring needed.
OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {
    "mode": Mode.parse,
    "autonomy": Autonomy.parse,
    "speed": Speed.parse,
    "branch_prefix": str,
    "privacy": str,
    "contribute": bool,
    "excluded_skills": _parse_excluded_skills,
    "loop_cadence_seconds": int,
    "require_human_approval_to_merge": bool,
    "require_human_approval_to_answer": bool,
    "ask_before_post_on_behalf": bool,
    "on_behalf_post_mode": OnBehalfPostMode.parse,
    "notify_user_via_bot": bool,
    "notify_on_post_on_behalf": bool,
    "user_identity_aliases": _parse_user_identity_aliases,
    "architectural_review_disabled": bool,
    "architectural_review_skill": str,
    "architectural_review_cadence_hours": int,
    "architectural_review_after_merge_count": int,
    "review_skill": str,
    "require_review_context": bool,
    "e2e_mandatory_gate_enabled": bool,
    "require_anti_vacuity_attestation": bool,
    "scanning_news_disabled": bool,
    "scanning_news_skill": str,
    "scanning_news_cadence_hours": int,
    "ask_before_creating_news_tickets": bool,
    "eval_local_disabled": bool,
    "eval_local_skill": str,
    "eval_local_cadence_hours": int,
    "dogfood_smoke_disabled": bool,
    "dogfood_smoke_skill": str,
    "dogfood_smoke_cadence_hours": int,
    "dogfood_smoke_overlay": str,
    "self_update_disabled": bool,
    "self_update_cadence_hours": int,
    "auto_update_reinstall": bool,
    "auto_update_require_green_main": bool,
    "resource_pressure_disabled": bool,
    "resource_pressure_cadence_minutes": int,
    "resource_pressure_min_free_interval_minutes": int,
    "disk_warn_free_gb": float,
    "disk_crit_free_gb": float,
    "ram_warn_avail_gb": float,
    "ram_crit_avail_gb": float,
    "disk_cache_allowlist": _parse_excluded_skills,
    "allow_destructive_disk": bool,
    "worktree_stale_days": int,
    "max_worktree_gc_per_tick": int,
    "allow_destructive_ram": bool,
    "ram_kill_allowlist": _parse_excluded_skills,
    "todo_sweep_disabled": bool,
    "todo_sweep_recheck_interval_hours": int,
    "max_concurrent_local_stacks": int,
    "clean_ignore": _parse_excluded_skills,
    "slack_voice_classifier_mode": SlackVoiceClassifierMode.parse,
    "pull_main_clone_disabled": bool,
    "pull_main_clone_cadence_hours": int,
    "review_nag_enabled": bool,
    "orchestrator_bash_gate_enabled": bool,
    "mr_title_regex": str,
    "issue_implementer_enabled": bool,
    "issue_implementer_label": str,
    "issue_implementer_max_concurrent": int,
    "issue_implementer_cadence_hours": int,
    "auto_disposition_enabled": bool,
    "auto_disposition_max_closes_per_tick": int,
}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_SPEED": ("speed", Speed.parse),
    "T3_ON_BEHALF_POST_MODE": ("on_behalf_post_mode", OnBehalfPostMode.parse),
    "T3_REVIEW_SKILL": ("review_skill", str),
    "T3_ISSUE_IMPLEMENTER_ENABLED": ("issue_implementer_enabled", _parse_env_bool),
    "T3_LOOP_AUTO_UPDATE": ("auto_update_reinstall", _parse_env_bool),
}


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

        This is the CLI-routing key only — distinct from the legacy-alias fold
        in :func:`_match_canonical_ep`, which maps a bare ``[overlays.<alias>]``
        table onto an installed entry point. Keep the two separate.
        """
        return name.removeprefix("t3-")


def _default_handover_mirror_path() -> Path:
    """Human-readable mirror of the latest session hand-off.

    ``${XDG_STATE_HOME:-~/.local/state}/teatree/handover/latest.md`` — XDG
    *state* (not data) because a hand-off is regenerable transient session
    state, not durable user data. Overridable via ``[teatree]
    handover_mirror_path``. The DB row is the source of truth; this file
    is for human-readability and for bootstrapping a brand-new session.
    """
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "teatree" / "handover" / "latest.md"


@dataclass
class UserSettings:
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    worktrees_dir: Path = field(default_factory=lambda: DATA_DIR / "worktrees")
    branch_prefix: str = ""
    privacy: str = ""
    check_updates: bool = True
    timezone: str = ""
    contribute: bool = False
    excluded_skills: list[str] = field(default_factory=list)
    redis_db_count: int = 16
    mode: Mode = Mode.INTERACTIVE
    autonomy: Autonomy = Autonomy.BABYSIT
    # How much parallel work the orchestrator drives at once. The
    # conservative ``MEDIUM`` baseline means NO orchestrator fan-out — only
    # the intrinsic loop + PR sweep + per-overlay ``max_concurrent_auto_starts``
    # provide throughput. ``slow`` caps to one impl worker; ``full`` arms the
    # /t3:speed loop; ``boost`` runs a single parallel-blast wave. Orthogonal
    # to ``mode``/``autonomy`` (those gate *whether* a publish proceeds; this
    # governs *how many* threads run) and never relaxes a safety gate.
    # Per-overlay overridable; ``T3_SPEED`` env wins over both.
    speed: Speed = Speed.MEDIUM
    # Loop tick interval in seconds (BLUEPRINT § 5.6). Default 12 minutes.
    loop_cadence_seconds: int = 720
    # Training-wheel for `auto` overlays: when true, the loop autonomously
    # pushes and creates PRs but stops short of merging — merge requires a
    # human reaction (👍 or `/merge`). The user flips this off only once
    # comfortable (BLUEPRINT § 5.6.2). No effect in `interactive` mode,
    # where every publishing action prompts regardless.
    require_human_approval_to_merge: bool = True
    # Training-wheel for the `t3:answerer` capability (#670, resolving
    # #654 Open Question #3): when true, the agent drafts a reply to an
    # inbound question, DMs the user for approval, and posts only on
    # confirmation. Set false to let the agent post answers directly — a
    # deliberate opt-in the user flips only once comfortable with answer
    # quality. Per-overlay overridable (a trusted overlay can opt into
    # direct posting without flipping the global). Default on, mirroring
    # `require_human_approval_to_merge`.
    require_human_approval_to_answer: bool = True
    # Pre-gate for posts the agent makes under the user's identity to a
    # colleague/customer surface (PR/MR comment, issue comment, Slack
    # channel/thread post, Notion post, PR/MR approve, reaction on
    # someone else's message).
    #
    # **Deprecated** in favour of the tri-state ``on_behalf_post_mode``
    # below. Kept on ``UserSettings`` for one release as a derived
    # computed value: ``True`` when the resolved mode is ``ASK`` or
    # ``DRAFT_OR_ASK``, ``False`` when ``IMMEDIATE``. The toml loader
    # still accepts ``[teatree] ask_before_post_on_behalf = true/false``
    # and translates it into the new mode (see ``load_config``) so
    # existing user configs keep working.
    ask_before_post_on_behalf: bool = True
    # Tri-state pre-gate over on-behalf colleague/customer posts (#960):
    #
    # * ``DRAFT_OR_ASK`` (default) — colleague-invisible, revocable draft
    #   notes (``t3 review post-draft-note``) publish autonomously and
    #   the agent DMs the user with the publish/delete commands; every
    #   other gated action collapses to BLOCK identical to ``ASK``.
    # * ``ASK`` — every gated action requires an explicit recorded
    #   approval (``t3 review approve-on-behalf``) before it publishes.
    # * ``IMMEDIATE`` — the gate is off; gated actions publish directly
    #   (subject to the always-gated list in ``Mode``).
    #
    # Backward-compat shim: if ``on_behalf_post_mode`` is absent but the
    # legacy ``ask_before_post_on_behalf`` is set, the loader resolves
    # the mode as ``ASK`` (true) / ``IMMEDIATE`` (false). The default
    # when neither is set is ``DRAFT_OR_ASK``.
    on_behalf_post_mode: OnBehalfPostMode = OnBehalfPostMode.DRAFT_OR_ASK
    # Pass --chrome to every spawned `claude` session so Claude in Chrome is
    # available wherever it could be useful (browser inspection, UI debugging,
    # E2E selector drafting, bug hunts). Costs ~300 lines of system prompt per
    # session; turn off only on machines without the Chrome extension.
    claude_chrome: bool = True
    # Whether teatree should append an agent identity (`Co-Authored-By`,
    # "Sent using …", "Generated with …") to artifacts published on the
    # user's behalf — git commits, PR descriptions and comments, Slack
    # messages, issue bodies. Default off: the user is the author, the agent
    # is the typist. Honored by every teatree post-on-behalf code path; the
    # rule for ad-hoc agent posting (MCP Slack, gh comment, etc.) lives in
    # `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the User's
    # Behalf".
    agent_signature: bool = False
    # Bot→user Slack notification channel (#963). When true, the helper
    # `teatree.notify.notify_user(...)` posts agent answers / questions /
    # important-info to the user's configured Slack DM via the bot identity,
    # auditing each send in the `BotPing` ledger. Out of scope of the
    # on-behalf gates (#960/#949): those govern posts the agent makes *as*
    # the user to colleagues/customers; this is the bot talking to its own
    # operator. Default on; turn off to keep notifications CLI-only.
    notify_user_via_bot: bool = True
    # After-receipt visibility DM (#949). When true (default), every
    # colleague-visible post the agent makes under the user's identity is
    # followed by a bot→user DM naming the destination, a clickable
    # artifact link, and a one-line summary — durable enforcement that
    # retires the per-session memory `notify-user-on-every-post-on-behalf`.
    # Distinct from the `on_behalf_post_mode` pre-gate (which decides
    # *whether* a post may publish): this fires *after* a successful
    # publish and never blocks or rolls back the post. Flip off via
    # `[teatree] notify_on_post_on_behalf = false`; per-overlay
    # overridable; intentionally NO env var (notify_user_via_bot, its
    # sibling, has none — a copied-by-analogy env layer would be a lie).
    # Out of scope: internal orchestration writes (bot→user DMs, the
    # loop's own bookkeeping) — only colleague-visible on-behalf posts.
    notify_on_post_on_behalf: bool = True
    # Derived under the ``notify`` tier by ``_apply_autonomy``; ORed with the field above.
    notify_on_behalf: bool = False
    statusline_chain: list[str] = field(default_factory=list)
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
    user_identity_aliases: list[str] = field(default_factory=list)
    # Solo vs collaborative working mode (issue #550 item 4). Empty string
    # = auto-detect from `git shortlog` history (see teatree.repo_mode);
    # an explicit "solo" / "collaborative" pins the verdict and bypasses
    # detection. Consumed by skills via `t3 tool repo-mode` so the
    # ask-first-vs-fix-proactively decision lives in one place, not in
    # every skill.
    repo_mode: str = ""
    # #1136 / #1152 Periodic architectural-review scanner — CORE
    # always-on (not per-overlay opt-in). The cadence applies uniformly
    # to every overlay's worktrees because it is a teatree-platform
    # behaviour. Set ``architectural_review_disabled = true`` in
    # ``[teatree]`` (or per-overlay) as the escape hatch.
    architectural_review_disabled: bool = False
    architectural_review_skill: str = "ac-reviewing-codebase"
    architectural_review_cadence_hours: int = 168
    architectural_review_after_merge_count: int = 25
    # #1539 Per-ticket deep-review skill. Empty = opt-in unset: the
    # reviewing-phase evidence gate (``teatree.core.gates.review_skill_gate``) is
    # a NO-OP, so projects that do not configure a review skill keep
    # recording the ``reviewing`` attestation unchanged. When set (e.g.
    # ``ac-reviewing-codebase``), ``lifecycle visit-phase <id> reviewing``
    # refuses to record the phase without durable evidence the skill ran.
    # Distinct from ``architectural_review_skill`` (the periodic cadence
    # scanner) — this one gates a single ticket's reviewing attestation.
    review_skill: str = ""
    # Opt-in deep-retrieval gate on ``-> reviewing`` (``review_context_gate``);
    # default false = NO-OP. Per-overlay overridable.
    require_review_context: bool = False
    # #1829 Opt-in SHA-bound anti-vacuity gate on review-request/merge
    # (``anti_vacuity_gate``); default false = NO-OP. Per-overlay overridable.
    require_anti_vacuity_attestation: bool = False
    # #1191 Periodic scanning-news scanner — CORE always-on with a daily
    # cadence (24h). Companion to the `scanning-news` skill (#1190): the
    # loop fires a `scanning_news` task daily so the news-scan workflow
    # runs without depending on an external cron. Set
    # ``scanning_news_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    scanning_news_disabled: bool = False
    scanning_news_skill: str = "scanning-news"
    scanning_news_cadence_hours: int = 24
    # #1391 Ask-gate for news-scan ticket creation. When true (default),
    # the scanning-news skill must NOT auto-create issues — it records a
    # ``PendingArticleSuggestion`` per candidate and surfaces the batch
    # to the user, filing an issue only on explicit approval. Default ON:
    # backlog pollution from unconfirmed auto-filing is the failure mode
    # this gate forecloses. Per-overlay overridable.
    ask_before_creating_news_tickets: bool = True
    # Periodic local-eval scanner — CORE always-on with a weekly cadence
    # (168h). User directive (2026-06-05): "AI evals should be run locally
    # from time to time, and in CI once a week." The loop fires an
    # ``eval_local`` task per cadence window so the SCOPED eval suite runs
    # locally via the no-API-key subscription runner (the same path
    # ``t3 eval run`` defaults to), without depending on an external cron.
    # Set ``eval_local_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    eval_local_disabled: bool = False
    eval_local_skill: str = "eval"
    eval_local_cadence_hours: int = 168
    # #1308 Periodic provision-smoke scanner — CORE always-on with a
    # 24h cadence by default. Queues a ``dogfood_smoke`` task per cadence
    # window so the loop exercises the active overlay's provision path
    # before the user reaches for E2E. Set
    # ``dogfood_smoke_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch. ``dogfood_smoke_overlay`` pins which overlay
    # anchor the placeholder task is created against — empty falls back
    # to the active overlay resolved via ``discover_active_overlay``.
    dogfood_smoke_disabled: bool = False
    dogfood_smoke_skill: str = "dogfood-smoke"
    dogfood_smoke_cadence_hours: int = 24
    dogfood_smoke_overlay: str = ""
    # #1249 Auto t3-update scanner — fast-forwards the editable teatree
    # clone + every registered overlay clone to ``origin/<default>`` once
    # the cadence has elapsed. Hourly default keeps the orchestrator
    # current without spamming the upstream remote on every tick. Set
    # ``self_update_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    self_update_disabled: bool = False
    self_update_cadence_hours: int = 1
    # ``T3_LOOP_AUTO_UPDATE`` env overrides ``auto_update_reinstall``;
    # ``auto_update_require_green_main`` fails closed on non-green default-branch CI.
    auto_update_reinstall: bool = False
    auto_update_require_green_main: bool = True
    # #128 Resource-pressure scanner — teatree-controlled auto-free before
    # the host hits OOM / full-disk. Measures ABSOLUTE free bytes
    # (``os.statvfs`` for disk, ``vm_stat`` reclaimable pages for RAM) — never
    # percent-of-nominal (the APFS shared-container total and macOS "99 % RAM
    # used" both mislead). Monitoring + regenerable-cache purge are on by
    # default; every irreversible lever (worktree GC, process SIGTERM) is
    # flag-gated OFF. ``resource_pressure_disabled = true`` is the durable
    # kill-switch (mirrors ``self_update_disabled``): the scanner is never
    # wired. All knobs are per-overlay overridable.
    resource_pressure_disabled: bool = False
    resource_pressure_cadence_minutes: int = 5
    resource_pressure_min_free_interval_minutes: int = 30
    disk_warn_free_gb: float = 25.0
    disk_crit_free_gb: float = 10.0
    ram_warn_avail_gb: float = 3.0
    ram_crit_avail_gb: float = 1.5
    # Allow-LIST only (never a denylist): exactly these regenerable cache dirs
    # are auto-purged at CRITICAL. ``uv`` is handled via ``uv cache prune``.
    # ``~/.cache/prek`` and ``~/.claude/projects`` are deliberately absent —
    # the latter is hard-protected even if a user adds it.
    disk_cache_allowlist: list[str] = field(
        default_factory=lambda: ["~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes"],
    )
    # Opt-in: enables stale-worktree GC (clean + fully pushed + unmodified
    # ``worktree_stale_days``) at CRITICAL, capped at
    # ``max_worktree_gc_per_tick`` per pass and never the active session's
    # worktree. Always logged + DM.
    allow_destructive_disk: bool = False
    worktree_stale_days: int = 30
    max_worktree_gc_per_tick: int = 3
    # Opt-in: enables SIGTERM (never SIGKILL) of allow-listed renderer
    # processes after >= 2 consecutive CRITICAL-RAM ticks, never a process in
    # the active-session ancestry. Empty ``ram_kill_allowlist`` means no
    # process is ever killed even when ``allow_destructive_ram = true``.
    allow_destructive_ram: bool = False
    ram_kill_allowlist: list[str] = field(default_factory=list)
    # #129 TODO-sweep scanner — per-overlay; verifies open Task rows against
    # their artifact's terminal state (issue closed / PR merged) and completes
    # only on durable proof, never in bulk and never on a stale read. On by
    # default; ``todo_sweep_disabled = true`` is the escape hatch.
    # ``todo_sweep_recheck_interval_hours`` is the per-task anti-thrash window
    # (a task swept within it is skipped this tick) and the idempotency window
    # for the atomic ``last_sweep_check_ts`` stamp.
    todo_sweep_disabled: bool = False
    todo_sweep_recheck_interval_hours: int = 1
    # #1397 Cap on concurrent locally-running stacks for a single overlay.
    # Each running worktree (``services_up``/``ready``) holds docker
    # containers, browsers, language servers, and CI processes — on a
    # memory-constrained host (one OOM observed 2026-05-27 when two stacks
    # ran in parallel), one stack at a time is the workable limit. The
    # ``t3 <overlay> worktree start`` / ``workspace start`` gate refuses to
    # advance a second stack into ``SERVICES_UP`` while another is already
    # there, naming the blockers and pointing at ``worktree teardown``.
    # Default ``0`` keeps the legacy unbounded behaviour so the gate is
    # opt-in; set ``1`` (or any positive integer) to enforce the cap.
    # Per-overlay overridable: a heavy overlay can cap to ``1`` while a
    # cheap dogfood overlay stays unbounded.
    max_concurrent_local_stacks: int = 0
    # fnmatch globs of branch names ``clean-all`` must NEVER reap even when the
    # squash-merge classifier says shipped — never-merge dev overrides, long-lived
    # spikes. Matched against the full branch name. Default empty: nothing
    # protected beyond the data-loss guards. Per-overlay overridable.
    clean_ignore: list[str] = field(default_factory=list)
    # #1395 Slack voice/token mismatch classifier. The pre-publish gate
    # between ``chat.postMessage`` and the Slack API refuses (or warns)
    # when the body's voice ("PR merged" / "evidence" → agent vs "please
    # review" / "RR for" → user) and the token kind it would go out under
    # (``xoxp-`` = user, ``xoxb-`` = bot) disagree on a confident case
    # (the recurrence: agent-voice DM via the personal token to the user's
    # own DM channel, which Slack does not notify on). ``warn`` is the
    # backward-compat default — log the mismatch but allow the post;
    # ``strict`` raises ``SlackVoiceMismatchError`` and refuses the post;
    # ``off`` disables the classifier entirely.
    slack_voice_classifier_mode: SlackVoiceClassifierMode = SlackVoiceClassifierMode.WARN
    # #2060 The resolved [teatree.speak] sub-table — a local playback enum
    # (off/dm/all) + a slack bool. See :class:`SpeakConfig` + blueprint §10.1.1.
    speak: SpeakConfig = field(default_factory=SpeakConfig)
    # The resolved [mr_reminder] slug→channel routing table for the
    # cross-repo "my open MRs" reminder; empty default keeps it inert.
    mr_reminder: MrReminderConfig = field(default_factory=MrReminderConfig)
    # #1398 Pre-publish close-trailer scanner. fnmatch patterns over
    # ``namespace/repo``: when an MR/PR target repo matches one of these
    # patterns and the body carries a ``Closes|Fixes|Resolves`` trailer,
    # the trailer line is silently stripped before publishing. Default
    # empty preserves legacy behaviour. Parsed from
    # ``[teatree.publish_gates] ban_close_trailers_on_namespaces``.
    ban_close_trailers_on_namespaces: list[str] = field(default_factory=list)
    # Pull-main-clone scanner — fast-forwards each work-repo *main clone*
    # under ``$T3_WORKSPACE_DIR`` to ``origin/<default>`` once the cadence
    # has elapsed, so a clone never drifts behind after a merge and
    # poisons ``git show`` / ``grep`` investigations. Hourly default keeps
    # the clones current without spamming each work repo's remote on every
    # tick. Set ``pull_main_clone_disabled = true`` in ``[teatree]`` (or
    # per-overlay) as the escape hatch.
    pull_main_clone_disabled: bool = False
    pull_main_clone_cadence_hours: int = 1
    # Fibonacci review-channel nag scanner (#1038). Ships DISABLED: a
    # concurrent-tick race on ``ReviewRequestPost.last_nag_step`` let two
    # sessions double-post bump replies into the colleague review channel,
    # including against already-merged MRs. Re-enable per-overlay via
    # ``[overlays.<name>].review_nag_enabled = true`` only after the
    # concurrency + merged-MR fixes are validated.
    review_nag_enabled: bool = False
    # Orchestrator-execution-boundary gate (#115, §17.6 gate 2). When
    # enabled (default), the main agent is blocked from running a HEAVY /
    # long-running foreground Bash command (test suite, build, dev
    # server, long sleep, full-tree sweep); ``run_in_background: true`` is
    # the escape hatch and sub-agents are unrestricted. The one-line
    # kill-switch ``[teatree] orchestrator_bash_gate_enabled = false``
    # disables the gate entirely (also read directly by the hook layer's
    # ``_orchestrator_bash_gate_enabled`` so a `t3 update` that reinstalls
    # the gate stays off until the user flips it back).
    orchestrator_bash_gate_enabled: bool = True
    # Mandatory-E2E FSM gate for customer-display-impacting changes (#1967).
    # When enabled (default), `pr create` and the §17.4 `ticket clear` refuse a
    # change the active overlay classifies as customer-display-impacting unless
    # recorded green E2E evidence exists at the reviewed tree OR a single-use
    # user-recorded `E2EBypassApproval` exists. Its OWN kill-switch — never a
    # reuse of another gate's switch: `[teatree] e2e_mandatory_gate_enabled =
    # false` (per-overlay overridable via `[overlays.<name>]`) disables it
    # entirely. The bypass is satisfiable per-tree only by the human user; a
    # maker/coding-agent/loop approver id is refused (maker≠checker).
    e2e_mandatory_gate_enabled: bool = True
    # Conventional-Commits title pattern enforced at ``pr create`` BEFORE the
    # gh/glab network call (#1540). A non-matching title is rejected with the
    # pattern printed verbatim; the description is independently required to
    # carry a What/Why header. Per-overlay overridable via
    # ``[overlays.<name>].mr_title_regex = "…"`` so an overlay with a different
    # title grammar declares its own pattern without flipping the global.
    mr_title_regex: str = DEFAULT_MR_TITLE_REGEX
    # #1548 Opt-in, default-OFF gate for the always-on issue-implementer
    # loop. The loop is a hard NO-OP unless ``issue_implementer_enabled``
    # is flipped on, mirroring the ``review_skill = ""`` opt-in (#1541) and
    # the ``scanning_news_*`` cadence pattern. This PR adds only the config
    # surface — the scanner and dispatch land in later PRs.
    issue_implementer_enabled: bool = False
    # Label marking an issue as auto-implement. Empty means no issue is
    # ever dispatched even when the loop is enabled (defence-in-depth: both
    # the master gate AND a non-empty label are required before any work
    # is picked up).
    issue_implementer_label: str = ""
    # Cap on simultaneously in-flight auto-implement tickets.
    issue_implementer_max_concurrent: int = 1
    # Internal dispatch-rate floor (hours) between auto-implement pickups.
    issue_implementer_cadence_hours: int = 1
    # #2122 Opt-in, default-OFF gate for the issue-disposition triage scanner.
    # When False (the default) no scanner is built, so the loop emits nothing
    # and never auto-closes an issue. The scanner only CLOSES high-confidence
    # dead noise (already-shipped / exact-duplicate / obsolete) — it is
    # physically unable to enqueue work, so flipping it on cannot grow the
    # backlog queue.
    auto_disposition_enabled: bool = False
    # Upper bound on close-candidate signals emitted per tick — keeps an
    # auto-close pass bounded and reviewable.
    auto_disposition_max_closes_per_tick: int = 5
    # Human-readable mirror of the latest session hand-off. The
    # ``SessionHandover`` DB row is the source of truth; this file mirrors
    # the payload for human-readability and for bootstrapping a brand-new
    # session. Default ``${XDG_STATE_HOME:-~/.local/state}/teatree/handover/
    # latest.md``; override via ``[teatree] handover_mirror_path``.
    handover_mirror_path: Path = field(default_factory=_default_handover_mirror_path)
    # Env kill-switch ``T3_ISSUE_IMPLEMENTER_ENABLED`` (operational fast-
    # disable) wins over both the per-overlay override and the global
    # setting; resolution is env → per-overlay ``[overlays.<name>]`` →
    # global ``[teatree]`` → this dataclass default.
    # SDK-equivalent cost reporting (``t3 cost``). Day-of-month the Agent-SDK
    # monthly credit refreshes; the billing cycle ``t3 cost`` totals against
    # starts on that day. ``0`` (default) means the refresh day is unknown, so
    # the cycle is the calendar month. ``sdk_monthly_credit_usd`` is the credit
    # the cycle-to-date spend is shown against ($200 = Max 20x).
    billing_cycle_anchor_day: int = 0
    sdk_monthly_credit_usd: float = 200.0


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)
