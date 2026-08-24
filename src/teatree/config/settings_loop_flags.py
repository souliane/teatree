"""The ``_LoopFlagAndCredentialSettings`` group base for ``UserSettings``.

Split out of ``teatree.config.settings`` for the module-health LOC cap (#1983).
Imported back into ``settings.py`` as one of ``UserSettings``'s declaration
bases — see that module's docstring for why the groups are inheritance bases
rather than composed attributes.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from teatree.config.setting_parsers import _default_handover_mirror_path


@dataclass
class _LoopFlagAndCredentialSettings:
    """Loop feature-flags (issue-implementer, fleet/orchestrate, outer/directive), cost + Anthropic pass routing."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "Kill switches & credentials")

    # #1548 The master gate for the always-on issue-implementer loop. Flipped ON by
    # #3895 (owner-authorised autonomous-by-default posture): the factory intakes
    # admitted issues without an operator opt-in. Flip OFF to make the loop a hard
    # NO-OP. The per-issue admission decision table still applies.
    issue_implementer_enabled: bool = True
    # #3634 The owner-applied ADMISSION label — the admit-label rule of the intake decision
    # table, and the label-scoped discovery query. It is the ONLY route by which an
    # UNTRUSTED author's issue reaches the factory; a trusted author needs no label
    # at all. Empty resolves to the shipped ``t3-auto`` convention
    # (``factory_admission.DEFAULT_ADMIT_LABEL``).
    issue_implementer_label: str = ""
    # #3235 The allowlist of OTHER humans whose issues the factory may act on (a
    # colleague, an operator account) — one of the three UNION sources of the
    # trusted-author set, alongside the owner's own ``user_identity_aliases`` and
    # the canonical ``TrustedIdentity`` rows. Resolved by
    # ``teatree.config.effective_trusted_issue_authors`` (config tier) and unioned
    # with the DB rows at ``teatree.core.review.author_trust``.
    #
    # SAFETY: this is an intake authority — an entry here can command the
    # autonomous factory by filing an issue. Default EMPTY, fail-closed: teatree
    # ships trusting NOBODY but the operator's own configured aliases, so an
    # unconfigured deployment can never auto-implement a stranger's issue. It
    # governs INTAKE only; merge authority is untouched (a substrate PR still
    # needs a recorded human approver).
    trusted_issue_authors: list[str] = field(default_factory=list)
    # The FALLBACK in-flight ceiling when the resource loop has no adaptive
    # opinion (kill-switch off, no reading yet, or stale) — see #3992's
    # resolve_intake_concurrency, which otherwise derives the live limit from
    # observed headroom and may exceed this number.
    issue_implementer_max_concurrent: int = 3
    # The intake scanner's OWN deadline for its candidate walk. Below the scan phase's
    # 60s pool deadline on purpose: past that one the thread is abandoned rather than
    # stopped, so it keeps mutating rows after the tick ended and records no resume
    # point for the next pass (#4466).
    issue_intake_pass_budget_seconds: float = 45.0
    # Marker labels for an UMBRELLA/epic parent intake never claims (#4105) — data, not a
    # constant, because which marker a deployment uses is its own policy. Emptying it
    # turns the LABEL half off; the structural half still declines an unlabelled epic.
    umbrella_issue_labels: list[str] = field(default_factory=lambda: ["epic", "umbrella", "tracking"])
    # Fleet-safety Stage 2 kill-switch (default OFF). When ON, the cross-instance
    # MUTEX (``teatree.core.fleet.claim`` — a GitHub claim ref as a server-side CAS)
    # governs the whole in-flight lifecycle: the issue-implementer dispatch WINS the
    # ref before granting a marker (the marker is a CACHE, not the authority); a
    # per-tick HEARTBEAT sweep re-affirms every in-flight claim so it can never
    # expire and be stolen mid-dispatch (a stolen claim ABANDONS the marker so the
    # work aborts); and every outward write is FENCED fail-closed against
    # ``is_held_by_me`` — the sync pre-ship gate, the async ``execute_ship`` (before
    # BOTH the branch push and the PR-open), and the orphan-branch PR-create.
    # (The §17.4 merge keystone fence is a scoped follow-up.) When OFF the behaviour
    # is byte-for-byte today's local-only get_or_create. If the ref infra is
    # unreachable while ON the claim/fence fails SAFE (does not claim / does not
    # push under an unconfirmable claim, logs loudly); turning the switch OFF
    # restores today's behaviour. DB-home (#1775), per-overlay overridable;
    # ``T3_FLEET_CLAIM_ENABLED`` env wins over both.
    fleet_claim_enabled: bool = False
    # #1796 / agent-teams Track-A PR#1: opt-in, default-OFF arm for the
    # dispatch loop's ``orchestrate_phase`` claim. The phase is wired dormant
    # (``claim=False``) in ``run_tick`` — it computes the deterministic fan-out
    # manifest from ``wip`` + ``max_concurrent_auto_starts`` but never claims
    # or spawns. When this is flipped on, the tick runs ``orchestrate_phase``
    # with ``claim=True`` so the lead does the thin per-unit claim+spawn the
    # manifest already computes (the #786-N4 claim-is-the-spawn boundary). When
    # off (the default) the dormant ``claim=False`` path is kept EXACTLY, so the
    # loop's behaviour is unchanged. Mirrors ``issue_implementer_enabled``;
    # per-overlay overridable and ``T3_ORCHESTRATE_CLAIM_ENABLED`` env wins over
    # both.
    orchestrate_claim_enabled: bool = False
    # T4-PR-1 — the OFF switch the autoresearch outer-loop runtime ships behind,
    # and the canonical first entry of the ``FEATURE_FLAGS`` lifecycle registry
    # (``config/feature_flags.py``, stage=DARK). Ships behaviorally inert: NOTHING
    # reads it in this PR — the later outer-loop runtime wires its scanner behind
    # ``get_effective_settings().outer_loop_enabled``, so the governed OFF switch
    # exists before the risky code lands. DB-home (#1775): resolved from the
    # ``ConfigSetting`` store (global + overlay rows); a ``[teatree]`` /
    # ``[overlays.<name>]`` TOML value is ignored on read. The conformance suite
    # pins stage=DARK => this default == its off_value (False), so the outer loop
    # can never be flipped default-ON without a code-reviewed stage demotion.
    outer_loop_enabled: bool = False
    # North-star PR-6 — the master gate for the directive-driven self-modification
    # front-end (intake + interpret + ratify), graduated DARK -> SETTLING by #3895.
    # Default ON: a captured directive IS interpreted, and the intake arc terminates at
    # the structural human ratify gate. The EXECUTION arc past that gate additionally
    # needs ``factory_score_enabled`` (default OFF) and a live critic, so nothing
    # self-modifies at default resolution. DB-home (#1775), per-overlay overridable —
    # flip OFF to disable directive intake entirely.
    directive_loop_enabled: bool = True
    # North-star PR-7 — the directive-loop VERIFYING horizon in days: after the ratified
    # activation is applied, the five evidence classes (activation live, acceptance green,
    # behavior probe clean, no collateral regression, zero open critic findings) are
    # judged once this many days elapse. DB-home, per-overlay overridable. Inert while
    # ``directive_loop_enabled`` is off (nothing reaches VERIFYING).
    directive_verify_days: int = 7
    # #3649 — how many directives one tick may advance through the INERT pre-admission
    # arc (interpret → clarify → ratify-ask → admit). Execution stays one directive per
    # tick regardless: this bounds only the arc that writes nothing and terminates at the
    # human ratify gate, so a backlog reaches the owner in a bounded number of ticks
    # instead of one directive per tick. DB-home, per-overlay overridable.
    directive_intake_per_tick: int = 25
    # T4-PR-3 — the autoresearch outer-loop runtime bounds (guard chain G4). Inert
    # while the flag is off: the measurement horizon after an experiment merges,
    # the max experiments admitted per rolling 7-day window, and the convergence
    # brake — after this many consecutive non-KEPT decisions the loop parks itself
    # (a DeferredQuestion) instead of proposing a fourth. Per-overlay overridable.
    outer_loop_measure_days: int = 7
    outer_loop_max_per_week: int = 1
    outer_loop_stop_after_consecutive_failures: int = 3
    # T4-PR-2 — the SIG-PR-2 recipe/score seam OFF switch (a DARK ``FEATURE_FLAGS``
    # entry). Ships OFF: ``t3 <overlay> recipe score`` still COMPUTES read-only (for
    # calibrating recipe weights against real ledger data pre-enable), but ``--record``
    # refuses, NO ``FactoryScoreSnapshot`` row is ever written, NO ``DeferredQuestion``
    # is queued, and ``build_server()`` does not register the MCP ``factory_score`` tool
    # — the outer loop physically has no metric surface. DB-home, per-overlay overridable.
    factory_score_enabled: bool = False
    # T4-PR-2 — the human-approved recipe sha (``config/factory_recipe.recipe_sha``).
    # A scored read stamps ``recipe_approved`` by comparing the committed recipe's sha
    # to this; unset (the default) means no recipe is approved, so every payload is
    # ``recipe_approved=false`` until a human runs ``t3 <overlay> recipe approve``.
    approved_recipe_sha: str = ""
    # PR-13 boost pool-refill target: how many live loop workers ``boost`` wip
    # keeps in flight. ``0`` (default) means UNSET — ``boost`` keeps today's
    # summed per-overlay ``max_concurrent_auto_starts`` target. A positive ``N``
    # makes the orchestrate planner refill to ``N`` each tick, clamped by the
    # PR-01 resource ceiling (``provision_max_concurrency`` / nCPU). DB-home,
    # per-overlay overridable, ``T3_BOOST_CONCURRENCY`` env wins; set via
    # ``t3 <overlay> wip boost N``.
    boost_concurrency: int = 0
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
    # Master gate for the needs-triage assessor loop. Default ON: the scanner
    # discovers OPEN needs-triage issues and queues ONE shell-denied assessment
    # task behind an ask-gate — it performs ZERO host writes and NOTHING acts
    # autonomously (per-item approval via t3:triaging-issues). Flip OFF to make the
    # loop emit nothing.
    triage_assessor_enabled: bool = True
    # Opt-in, default-OFF gate for the MR-triage surveyor. When False (the default)
    # no scanner is built, so the loop emits nothing. When on, the scanner walks the
    # operator's own open MRs, runs each through the pure triage ladder, and SURFACES
    # the verdict -- it posts nothing and dispatches nothing, so turning it on cannot
    # produce a colleague-visible action.
    mr_triage_enabled: bool = False
    # Upper bound on verdicts surfaced per tick -- keeps one pass reviewable.
    mr_triage_max_mrs_per_tick: int = 20
    # Min interval between assessment passes (the scanner self-gates on this).
    triage_assessor_cadence_hours: int = 24
    # Upper bound on issues serialized into one queued assessment task — keeps the
    # batch bounded and the DM reviewable.
    triage_assessor_max_issues_per_tick: int = 10
    # Directive #2 — the periodic DB-backup scanner's config surface (the knobs
    # ship ahead of the Unit-18 scanner that reads them, so a later PR wires the
    # loop behind a governed, tested config seam rather than adding knobs and
    # behaviour in one risky change). ``db_backup_disabled`` is the escape-hatch
    # kill-switch (default OFF = the scanner runs once wired); ``db_backup_cadence_hours``
    # is the min interval between backup passes; ``db_backup_retention_days`` is how
    # long a backup artifact is kept before the pass prunes it. A non-positive
    # cadence / retention FAILS SAFE to the default at read time (see the registry
    # parsers) so the "keep at least a week of backups" bound cannot be mistyped
    # away to 0 (which would prune every backup immediately). All three are
    # DB-home, per-overlay overridable.
    db_backup_disabled: bool = False
    db_backup_cadence_hours: int = 24
    db_backup_retention_days: int = 7
    # Directive #3 — idle usage-window auto-recovery, a SETTLING ``FEATURE_FLAGS``
    # entry (graduated DARK->SETTLING by #3691, default ON). When ON (the default) a
    # Claude usage-window limit (~5h session / 7-day weekly) PARKS the task (returns it
    # to the queue with a ``not_before`` at the window's re-arm instant) instead of
    # failing, an admission guard quietly parks further LLM dispatches on the exhausted
    # lane, and the self-rescheduling ``usage_window_recovery`` loop-timer chain clears
    # the window + releases the parked tasks + pumps the loop at reset — unattended, no
    # OS cron. So a fresh deploy self-recovers from an exhausted usage window rather than
    # idling until a human intervenes. OFF restores the pre-graduation behaviour: a limit
    # is recorded as a terminal FAILED attempt — no park, no admission guard, no recovery
    # chain. Survives as a per-overlay escape hatch during the soak. DB-home (#1775),
    # per-overlay overridable.
    limit_autorecovery_enabled: bool = True
    # #3201 PR-3b — the OFF switch the CI-eval self-heal AUTONOMOUS FIXER ships
    # behind, and a DARK ``FEATURE_FLAGS`` entry. Ships behaviorally inert: the
    # ``ci_eval_heal`` loop stays OBSERVE-ONLY (dispatch a behavioral eval, poll,
    # GREEN or HALT+escalate on any red — never a fix) exactly as PR-3a, UNLESS
    # this flag is on AND the ``ci_eval_heal`` ``Loop`` row is enabled (BOTH gates
    # required). When armed, a behavioral (non-infra) red triggers a BOUNDED
    # autonomous fixer dispatch (``begin_fix`` -> a headless coding sub-agent whose
    # pushed diff must pass the #3282 anti-cheat gate), capped at the session's
    # ``max_fix_attempts`` (default 2 at open time) before it HALTs and escalates —
    # never a loop-forever. The anti-cheat invariant is untouched: a genuinely
    # failing eval can NEVER be marked green, a fixer that edits a scenario/matcher
    # file is REJECTED, and an exhausted-budget session HALTs rather than greens.
    # DB-home (#1775), per-overlay overridable — an overlay can trial the fixer on
    # its own budget. The conformance suite pins stage=DARK => this default == its
    # off_value (False), so autonomous CI mutation can never ship default-ON without
    # a code-reviewed stage demotion.
    ci_eval_heal_autofix_enabled: bool = False
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
    # #2697 — formerly env-only bypass readers, now DB-home (#1775): each resolves
    # from the ``ConfigSetting`` store + its ``T3_*`` env layer where one is
    # registered in ``ENV_SETTING_OVERRIDES``, never from a bespoke
    # ``os.environ.get`` read. Set via ``t3 <overlay> config_setting set <key>``.
    #
    # GitLab-approval poll scanner (formerly ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED``).
    # Default off — poll-driven and overlapping with the webhook path.
    gitlab_approval_scanner_enabled: bool = False
    # Pass ``--plugin-dir`` to the launched Claude Code agent so retro may edit
    # core plugin files (formerly ``T3_CONTRIBUTE``). ``T3_CONTRIBUTE`` env wins.
    contribute_plugin_dir: bool = False
    # Enable the dream command's eval-proposal phase on the manual ``run`` path
    # (formerly ``T3_DREAM_PROPOSE_EVALS``). The cadence-driven ``tick`` path has
    # its own seam and does not route through this field.
    dream_propose_evals: bool = False
    # Fetch PR/issue titles to enrich a prompt before trigger matching (formerly
    # ``T3_HOOK_FETCH_TITLES``). Default on. ``T3_HOOK_FETCH_TITLES`` env wins;
    # the UserPromptSubmit hook runs pre-Django, so there the DB tier is skipped
    # (fail-safe) and env + this default govern — identical to legacy behaviour.
    hook_fetch_titles: bool = True
    # Per-account ``pass`` routing for the two Anthropic credentials
    # (``teatree.llm.credentials``): an ORDERED LIST of ``pass`` entries the routing
    # selector (``teatree.credential_config.PassPathSelector``) fans out over per
    # overlay — it picks the first non-exhausted account (sticky, with cross-account
    # fallback), so the subscription OAuth token / metered API key read from a
    # per-account entry (e.g. ``anthropic/<account>/oauth-token``) with no code edit.
    # Empty (the default) means "no account configured". Neither credential has a
    # built-in default ``pass`` path, so an empty list + no env var makes resolution
    # fail loud (naming the setting), never a dead default. DB-home (#1775): the
    # selector reads the list off the ``ConfigSetting`` store at RESOLVE time via
    # ``ConfigSetting.objects.get_effective`` (overlay scope then global), so
    # per-overlay routing works, and ``get_effective_settings()`` reports ``[]`` when
    # unset. Set via ``t3 <overlay> config_setting set
    # anthropic_oauth_pass_paths '["anthropic/<account>/oauth-token"]'``.
    anthropic_oauth_pass_paths: list[str] = field(default_factory=list)
    anthropic_api_key_pass_paths: list[str] = field(default_factory=list)
