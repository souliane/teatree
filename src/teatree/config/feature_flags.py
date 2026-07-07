"""Feature-flag discrimination over ``UserSettings`` bool fields (T4-PR-1).

A *setting* is a durable knob the operator tunes and keeps. A *feature flag* is a
TEMPORARY switch that gates in-flight code and is meant to DIE — it is born with
the code it guards and removed once that code is trusted (or abandoned). Both
resolve their VALUE identically through the untouched
``env -> ConfigSetting(overlay) -> ConfigSetting(global) -> dataclass-default``
chain; this registry only DISCRIMINATES a flag from a setting, GOVERNS its
lifecycle stage, and AUDITS dead toggles.

The registry lives in CODE (a flag is born and dies with the code it gates); only
the flag's VALUE lives in the ``ConfigSetting`` store. This is the
typed-registry-plus-fitness-test idiom of ``SETTING_HOMES`` / ``COLD_HOOK_SETTINGS``
/ ``BOOTSTRAP_FILE_ONLY_SETTINGS``: the registry is pure data, and the conformance
suite in ``tests/config/test_feature_flags.py`` keeps every entry honest — each
must name a real ``bool`` ``UserSettings`` field registered in
``OVERLAY_OVERRIDABLE_SETTINGS``, carry a non-empty ``tracking_issue`` and a valid
``stage``, and (for a ``DARK`` flag) default to its own ``off_value`` so a dark
feature can never ship default-ON without a code-reviewed stage demotion.
"""

from dataclasses import dataclass
from enum import StrEnum

# The loud banner the audit view prints for a ``REMOVE``-stage flag: the gated
# code is permanent, so the toggle is dead weight whose only job left is deletion.
REMOVE_STAGE_BANNER = "DEAD TOGGLE — REMOVE (gated code is permanent)"


class FlagStage(StrEnum):
    """A feature flag's position in its birth-to-death lifecycle.

    ``DARK`` — the gated code is in flight and ships OFF; the flag exists so the
    code can land dark and be enabled per-install for a deliberate trial.
    ``SETTLING`` — the gated code is trusted and default-enabling is imminent; the
    flag survives only as an escape hatch during the soak.
    ``REMOVE`` — the gated code is permanent; the flag is a dead toggle whose only
    remaining job is to be deleted (the audit view surfaces it loud).
    """

    DARK = "dark"
    SETTLING = "settling"
    REMOVE = "remove"


@dataclass(frozen=True)
class FeatureFlag:
    """Lifecycle metadata for one ``UserSettings`` bool field used as a feature flag.

    ``field`` is the ``UserSettings`` field name (equal to this entry's key in
    :data:`FEATURE_FLAGS`, pinned by the conformance suite). ``off_value`` is the
    value that means "gated code stays OFF" — ``False`` for a positive-sense
    ``*_enabled`` flag, ``True`` for an inverted-sense ``*_disabled`` flag — so the
    dark-defaults-off invariant reads correctly for both senses.
    """

    field: str
    stage: FlagStage
    tracking_issue: str
    summary: str
    off_value: bool = False


# ``outer_loop_enabled`` is the canonical DARK flag (the OFF switch the T4
# autoresearch outer loop ships behind). The live registry is currently all-``DARK``
# (PR-28 graduated the sole ``SETTLING`` flag ``loop_runner_enabled`` out — it became
# a durable operational kill-switch, not a dying flag), so the stage-discrimination
# machinery (:func:`dark_flags`, :func:`render_flags_audit`) is proven non-vacuously
# over a MIXED FIXTURE in the conformance suite rather than over the live set's
# accidental composition.
FEATURE_FLAGS: dict[str, FeatureFlag] = {
    "outer_loop_enabled": FeatureFlag(
        field="outer_loop_enabled",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — autoresearch outer-loop (T4)",
        summary="The OFF switch the T4 autoresearch outer-loop runtime ships behind.",
    ),
    "factory_score_enabled": FeatureFlag(
        field="factory_score_enabled",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — autoresearch outer-loop (T4)",
        summary="The SIG-PR-2 recipe/score seam; ships dark until the outer loop consumes the metric.",
    ),
    "teams_enabled": FeatureFlag(
        field="teams_enabled",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree#1838",
        summary="Agent-teams WORK layer; ships dark until a pane-backed teammate lands.",
    ),
    "require_plan_adequacy": FeatureFlag(
        field="require_plan_adequacy",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — SELFCATCH-3 plan_gate hardening",
        summary="Plan-adequacy + late-bound-plan gate; ships dark until the planner emits manifests.",
    ),
    "critic_gate_live": FeatureFlag(
        field="critic_gate_live",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — SELFCATCH-5 critic_gate v1",
        summary="Autonomous user-proxy critic on mark_delivered; ships dark (advisory: records, never blocks).",
    ),
    "directive_loop_enabled": FeatureFlag(
        field="directive_loop_enabled",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — north-star PR-6 directive intake",
        summary="The OFF switch the directive self-modification front-end (intake+interpret+ratify) ships behind.",
    ),
    "ambient_directive_detection_enabled": FeatureFlag(
        field="ambient_directive_detection_enabled",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree#116 — SEC-CONTEXT-FIREWALL",
        summary=(
            "The OFF switch for ambient detection of inbound untrusted DIRECTIVE-intent events; decoupled from "
            "directive_loop_enabled so arming the explicit loop never silently arms ambient (trifecta precondition)."
        ),
    ),
    "require_debt_delta": FeatureFlag(
        field="require_debt_delta",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — north-star PR-3 debt_delta_gate",
        summary="Deterministic no-new-tech-debt merge gate in _run_ship_gates; ships dark until an overlay opts in.",
    ),
    "require_executed_repro": FeatureFlag(
        field="require_executed_repro",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree#118",
        summary="Executed RED->GREEN repro gate on ship() for FIX tickets; ships dark until an overlay opts in.",
    ),
    "require_merge_quality_verdict": FeatureFlag(
        field="require_merge_quality_verdict",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — north-star PR-4 merge-quality critic",
        summary=(
            "Merge-quality (test_value + cleanliness) verdict gate on execute_bound_merge for ORDINARY tickets; "
            "directive tickets are gated unconditionally, so this flag ships dark until an overlay opts in."
        ),
    ),
    "design_critic_live": FeatureFlag(
        field="design_critic_live",
        stage=FlagStage.DARK,
        tracking_issue="souliane/teatree — north-star PR-5 design critic",
        summary=(
            "The design critic's transition=plan generic-vs-hack judgment for directive tickets; advisory "
            "(records CriticFinding, never blocks — mechanism_conforms is the teeth), ships dark."
        ),
    ),
}


def is_feature_flag(key: str) -> bool:
    """True when *key* is a governed feature flag rather than a durable setting."""
    return key in FEATURE_FLAGS


def dark_flags(flags: dict[str, FeatureFlag] | None = None) -> dict[str, FeatureFlag]:
    """The subset of *flags* (the live registry by default) still in the ``DARK`` stage.

    The query hook a later self-catching critic uses to tie a dark flag to the
    done-means-merged status of the code it gates. Pure over its argument (like
    :func:`render_flags_audit`) so the stage-filtering is proven non-vacuously over a
    mixed fixture even when the live registry happens to be single-stage.
    """
    registry = FEATURE_FLAGS if flags is None else flags
    return {key: flag for key, flag in registry.items() if flag.stage is FlagStage.DARK}


def flag_trailer(key: str) -> str:
    """The ``[feature flag, …]`` governance trailer for *key*, or ``""`` for a setting.

    Appended by ``config_setting set``/``get`` so an operator touching a flag key
    sees at a glance that they are flipping a governed, lifecycle-staged toggle —
    not a durable setting — and where its removal is tracked.
    """
    flag = FEATURE_FLAGS.get(key)
    if flag is None:
        return ""
    return f"[feature flag, stage={flag.stage.value}, tracking {flag.tracking_issue}]"


def render_flags_audit(flags: dict[str, FeatureFlag]) -> str:
    """Render the read-only dead-toggle audit report for *flags*.

    One line per flag naming its stage, off-value and tracking issue; a
    ``REMOVE``-stage flag is surfaced LOUD (:data:`REMOVE_STAGE_BANNER`) so a dead
    toggle cannot hide as a decorative registry entry. Pure over its argument so
    the conformance suite can prove the loud path with a ``REMOVE`` fixture without
    a ``REMOVE`` flag in the live registry.
    """
    if not flags:
        return "  (no feature flags registered)"
    lines: list[str] = []
    for key in sorted(flags):
        flag = flags[key]
        loud = f"  <<< {REMOVE_STAGE_BANNER} >>>" if flag.stage is FlagStage.REMOVE else ""
        lines.append(
            f"  {key}: stage={flag.stage.value}, off_value={flag.off_value}, "
            f"tracking {flag.tracking_issue}{loud}\n      {flag.summary}"
        )
    return "\n".join(lines)
