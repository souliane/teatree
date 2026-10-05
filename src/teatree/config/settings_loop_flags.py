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
    # opinion (no reading yet, or stale) — see #3992's
    # resolve_intake_concurrency, which otherwise derives the live limit from
    # observed headroom and may exceed this number.
    issue_implementer_max_concurrent: int = 3
    # Marker labels for an UMBRELLA/epic parent intake never claims (#4105) — data, not a
    # constant, because which marker a deployment uses is its own policy. Emptying it
    # turns the LABEL half off; the structural half still declines an unlabelled epic.
    umbrella_issue_labels: list[str] = field(default_factory=lambda: ["epic", "umbrella", "tracking"])
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
    # Directive #2 — the periodic DB-backup scanner's config surface.
    # ``db_backup_retention_days`` is how long a backup artifact is kept before the pass
    # prunes it. A non-positive retention FAILS SAFE to the default at read time (see the
    # registry parsers) so the "keep at least a week of backups" bound cannot be mistyped
    # away to 0 (which would prune every backup immediately). The cadence is the Loop
    # row's ``daily_at`` anchor, not a second setting. DB-home, per-overlay overridable.
    db_backup_retention_days: int = 7
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
    # Pass ``--plugin-dir`` to the launched Claude Code agent so retro may edit
    # core plugin files (formerly ``T3_CONTRIBUTE``). ``T3_CONTRIBUTE`` env wins.
    contribute_plugin_dir: bool = False
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
