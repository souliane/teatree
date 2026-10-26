"""Communication and on-behalf publishing settings."""

from dataclasses import dataclass
from typing import ClassVar

from teatree.config.declaration import SettingSpec, setting
from teatree.config.setting_parsers import _parse_str_list, _parse_strict_bool, _parse_strict_int, _parse_strict_str


@dataclass
class _OnBehalfSettings:
    """Human-approval training wheels + the on-behalf post gate + bot→user notify flags."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Communication", "Posting on your behalf")

    review_nag_max_interval_days: int = setting(
        30, spec=SettingSpec(help="ceiling on the review nag's widening re-ask interval", parse=_parse_strict_int)
    )

    # Training-wheel for `auto` overlays: when true, the loop autonomously
    # pushes and creates PRs but stops short of merging — merge requires a
    # human reaction (👍 or `/merge`). The user flips this off only once
    # comfortable (BLUEPRINT § 5.6.2). No effect in `interactive` mode,
    # where every publishing action prompts regardless. NOT tier-governed
    # (#3630): no `autonomy` value collapses it, so removing review before
    # merge is always a deliberate, separately-named opt-in.
    require_human_approval_to_merge: bool = setting(
        default=True,
        spec=SettingSpec(help="require a recorded human approver before a merge", parse=_parse_strict_bool),
    )
    # Whether the standing grant may sign off a SUBSTRATE merge (#3223). Default
    # off: a substrate CLEAR (merge keystone, architecture spec, governance doc,
    # self-guardrail seam) PINGS-and-HOLDS for the owner's per-PR sign-off even at
    # `autonomy = full` — the #2727 safety posture. Turning this on lets
    # `_overlay_grants_standing_substrate_signoff` cover a substrate CLEAR on an
    # overlay standing at `autonomy = full` (the solo-owned tier), so the owner's
    # own green PRs self-authorize substrate merges the same way non-substrate
    # clears already do. This changes only WHO authorizes the sign-off; the
    # quality/safety floor (independent cold review, reviewed-SHA bind, CI-green,
    # not-draft, maker≠checker, anti-vacuity) is untouched and still runs. The
    # `full` tier gate is kept so a below-full overlay never self-merges substrate
    # even with this on. DB-home (#1775), per-overlay overridable.
    substrate_self_signoff: bool = setting(
        default=False,
        spec=SettingSpec(help="let a substrate change be signed off by its own author", parse=_parse_strict_bool),
    )
    # The owner id the headless loop presents as the standing substrate merge
    # authorization (#3413). Empty (the default) preserves the hold-for-owner
    # posture verbatim — a substrate CLEAR PINGS-and-HOLDS and is never
    # auto-merged (invariant 4). Setting it to an owner id is the durable,
    # revocable delegation: the config WRITE is the human authorization. When
    # set, the `pr_sweep` scanner presents this id at merge time as the
    # `--human-authorized` a substrate CLEAR requires, and the keystone
    # (`_config_standing_substrate_delegation`) authorizes the merge ONLY when the
    # presented id still equals this configured value — sourced from config, never
    # a live CLI flag, so unsetting it revokes the delegation at the next merge.
    # Every gate still runs (green required checks, recorded merge_safe verdict,
    # clean rebase, draft-lock, maker≠checker, SHA-bind); this changes only WHO
    # supplies the substrate authorization (a standing config delegation vs. a
    # per-PR recorded human approval), and the merge is audited as config-sourced
    # (`MergeAudit.standing_delegation_by`) to stay distinguishable from an
    # interactive human authorization. DB-home (#1775), per-overlay overridable.
    substrate_auto_merge_authorized_by: str = setting(
        "", spec=SettingSpec(help="who authorized substrate changes to auto-merge", parse=_parse_strict_str)
    )
    # Per-(repo, ticket) open-PR budget: the max number of concurrently-open
    # (not-merged) PRs a single ticket may have in one repo. Enforced at the
    # core PR-creation seam by ``pr_budget_gate`` before a PR is opened. The
    # shipped default is ``1`` — "at most one open PR per repo per ticket" — so
    # the one-ticket-one-PR discipline holds out of the box (a stray second PR
    # for the same ticket in the same repo is a recurring cleanup cost, D9). Set
    # ``0`` to restore the unlimited opt-out. Constraint-as-data: the scope is a
    # per-overlay ``ConfigSetting`` row, never a branch in core code, so an
    # overlay wanting a different value needs no code change. DB-home (#1775);
    # per-overlay overridable.
    max_open_prs_per_repo_per_ticket: int = setting(
        1, spec=SettingSpec(help="open pull requests one ticket may hold in one repo", parse=_parse_strict_int)
    )
    # Training-wheel for the `t3:answerer` capability (#670, resolving
    # #654 Open Question #3): when true, the agent drafts a reply to an
    # inbound question, DMs the user for approval, and posts only on
    # confirmation. Set false to let the agent post answers directly — a
    # deliberate opt-in the user flips only once comfortable with answer
    # quality. Per-overlay overridable (a trusted overlay can opt into
    # direct posting without flipping the global). Default on, mirroring
    # `require_human_approval_to_merge`.
    require_human_approval_to_answer: bool = setting(
        default=True,
        spec=SettingSpec(
            help="require the operator to approve an answer before it is posted", parse=_parse_strict_bool
        ),
    )
    # Carve-out from the on-behalf pre-gate: actions in this allowlist resolve
    # to PROCEED even under a forbidding egress posture, because they are the user's
    # routine self-documentation on their OWN ticket (E2E evidence), not a
    # colleague-facing voice that needs the user's per-post approval. Default
    # includes ``post_e2e_evidence`` so the user never has to approve their own
    # evidence posts; clear the list (``on_behalf_auto_actions = []``) to
    # re-gate evidence under a forbidding posture. Per-overlay overridable; env
    # ``T3_ON_BEHALF_AUTO_ACTIONS`` (comma-separated) wins over both.
    on_behalf_auto_actions: list[str] = setting(
        default_factory=lambda: ["post_e2e_evidence"],
        spec=SettingSpec(
            help="actions the agent may take under the operator's identity unattended", parse=_parse_str_list
        ),
    )
    # Notion ids the integration token may write under; with none configured every write is refused.
    notion_write_allowed_roots: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help=("Notion ids the integration token may write under; with none, every write is refused"),
            parse=_parse_str_list,
        ),
    )
    # Notion ids never written under, even when an allowed root sits above them.
    notion_write_denied_roots: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(help="Notion ids never written under, even below an allowed root", parse=_parse_str_list),
    )
