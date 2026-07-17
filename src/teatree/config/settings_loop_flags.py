"""The ``_LoopFlagAndCredentialSettings`` group base for ``UserSettings``.

Split out of ``teatree.config.settings`` for the module-health LOC cap (#1983).
Imported back into ``settings.py`` as one of ``UserSettings``'s declaration
bases — see that module's docstring for why the groups are inheritance bases
rather than composed attributes.
"""

from dataclasses import dataclass, field
from pathlib import Path

from teatree.config.setting_parsers import _default_handover_mirror_path


@dataclass
class _LoopFlagAndCredentialSettings:
    """Loop feature-flags (issue-implementer, fleet/orchestrate, outer/directive), cost + Anthropic pass routing."""

    # #1548 Opt-in, default-OFF gate for the always-on issue-implementer
    # loop. The loop is a hard NO-OP unless ``issue_implementer_enabled``
    # is flipped on, mirroring the ``review_skill = ""`` opt-in (#1541) and
    # the ``scanning_news_*`` cadence pattern. This PR adds only the config
    # surface — the scanner and dispatch land in later PRs.
    issue_implementer_enabled: bool = False
    # Label marking an issue as auto-implement. Consulted ONLY when
    # ``issue_implementer_require_label`` is on — since #3235 intake is decided by
    # the issue's trusted AUTHOR, not by a hand-applied label. With the flag on,
    # an empty label means no issue is ever dispatched even when the loop is
    # enabled (defence-in-depth: the master gate AND a non-empty label are both
    # required before any work is picked up).
    issue_implementer_label: str = ""
    # #3235 Opt-in, default-FALSE: restore the pre-#3235 label filter as a
    # MANDATORY second gate on top of author trust. OFF (the default) means the
    # label is NOT required — every open issue authored by a TRUSTED author is
    # intaken, because the owner will not hand-tag tickets. The label filter can
    # only ever NARROW intake; it never widens it, and it can never launder an
    # untrusted author (the per-issue author gate refuses those regardless).
    issue_implementer_require_label: bool = False
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
    # Cap on simultaneously in-flight auto-implement tickets.
    issue_implementer_max_concurrent: int = 1
    # Internal dispatch-rate floor (hours) between auto-implement pickups.
    issue_implementer_cadence_hours: int = 1
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
    # North-star PR-6 — the OFF switch the directive-driven self-modification front-end
    # (intake + interpret + ratify) ships behind, and a DARK ``FEATURE_FLAGS`` entry.
    # Ships behaviorally inert: the ``DIRECTIVE``-intent router is parity-off while this
    # is off (a directive event DROPs exactly as an unrouteable intent), so nothing
    # writes a ``Directive`` row unless the explicit ``t3 <overlay> directive capture``
    # CLI is used. DB-home (#1775), per-overlay overridable — an overlay can trial
    # directive intake on its own budget. The conformance suite pins stage=DARK => this
    # default == its off_value (False), so it can never ship default-ON without a
    # code-reviewed stage demotion.
    directive_loop_enabled: bool = False
    # North-star PR-7 — the directive-loop VERIFYING horizon in days: after the ratified
    # activation is applied, the five evidence classes (activation live, acceptance green,
    # behavior probe clean, no collateral regression, zero open critic findings) are
    # judged once this many days elapse. DB-home, per-overlay overridable. Inert while
    # ``directive_loop_enabled`` is off (nothing reaches VERIFYING).
    directive_verify_days: int = 7
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
    # Opt-in, default-OFF gate for the needs-triage assessor loop. When False (the
    # default) no scanner is built, so the loop emits nothing and never queues an
    # assessment. When on, the scanner discovers OPEN needs-triage issues and queues
    # ONE shell-denied assessment task behind an ask-gate — it performs ZERO host
    # writes and NOTHING acts autonomously (per-item approval via t3:triaging-issues).
    triage_assessor_enabled: bool = False
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
    # Directive #3 — the OFF switch for idle usage-window auto-recovery, and a DARK
    # ``FEATURE_FLAGS`` entry. When OFF (the default) a Claude usage-window limit
    # (~5h session / 7-day weekly) is recorded as a terminal FAILED attempt EXACTLY as
    # today — no park, no admission guard, no recovery chain (behaviorally inert). When
    # ON, a limit hit PARKS the task (returns it to the queue with a ``not_before`` at
    # the window's re-arm instant) instead of failing, an admission guard quietly parks
    # further LLM dispatches on the exhausted lane, and the self-rescheduling
    # ``usage_window_recovery`` loop-timer chain clears the window + releases the parked
    # tasks + pumps the loop at reset — unattended, no OS cron. DB-home (#1775),
    # per-overlay overridable. The conformance suite pins stage=DARK => this default ==
    # its off_value (False), so it can never ship default-ON without a code-reviewed
    # stage demotion.
    limit_autorecovery_enabled: bool = False
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
