"""What KIND of key this is — the one classifier every per-key decision reads (B11).

Five registries each answered a slice of that question, and every decision that needed the
whole answer re-derived it by hand: ask-vs-delete, the existence-scalar split, the inert-gate
arm. Hand-derivation is why two surfaces could disagree about whether a key was a durable
setting or a governed toggle, and why "which class is this" had no reproducible answer.

This is a VIEW, not a sixth registry. Each class delegates to the registry that already owns
it, so a flag's lifecycle stays in :data:`~teatree.config.feature_flags.FEATURE_FLAGS` and a
gate's satisfier in :data:`~teatree.config.gate_evidence.GATE_EVIDENCE` — a key's class is
declared exactly once, where the payload that makes it that class already lives. Moving the
data here would have created the second source B11 exists to remove.

Two things follow from being a view, and both are pinned by
``tests/config/test_setting_taxonomy.py``. A key can hold SEVERAL classes at once — ten keys
are both a gate and a feature flag, so a single-class answer would be a lie. And
:attr:`SettingClass.PLAIN` is computed, never declared: it is what no registry claimed, so a
key cannot be plain and something else, and nobody maintains a list of the 191 ordinary keys.

The domain is every key that EXISTS, which is wider than the live union: a retired key is
deliberately absent from ``ALL_KNOWN_CONFIG_SETTINGS`` (it names no live field), so a view
over the live union alone would answer "unknown" for exactly the keys whose whole job is to
answer for themselves.

B9's read level is NOT here. Levels 1-2 are the schedule and preset selectors, and level 3 is
loop ownership — which is structure the settings-to-loop map builds, not a per-key label. A
``level`` field would answer "other" for 284 of 286 keys today, which is a field with no
reader rather than a classification.
"""

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from functools import cache

from teatree.config.feature_flags import FEATURE_FLAGS, FeatureFlag
from teatree.config.gate_evidence import GATE_EVIDENCE, GateEvidence
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS, REGISTRY_KEYS
from teatree.config.retired_settings import RETIRED_SETTINGS, RetiredSetting
from teatree.config.secret_settings import is_pass_key_setting
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS

#: The one place a gate ARMING switch is recognised. ``GATE_EVIDENCE`` models what proves a
#: gate RAN, and an arming switch has no such artifact by construction — it is what decides
#: whether the gate runs at all — so a switch is name-shaped rather than declared, and this
#: is the only class whose membership a name decides.
GATE_SWITCH_GLOBS = ("*_gate_enabled", "require_*")


def is_gate_switch(key: str) -> bool:
    """Whether *key* arms or disarms a gate."""
    return any(fnmatch(key, glob) for glob in GATE_SWITCH_GLOBS)


class UnclassifiedSettingError(KeyError):
    """Raised for a key that is neither a live setting nor a recorded retirement."""


class SettingClass(StrEnum):
    """How a key is GOVERNED — orthogonal to the shareability and storage axes.

    ``SettingMeta`` on the schema field already declares where a key is stored and whether
    its value is shareable. This axis answers the different question every deletion, arming
    and dashboard-label decision actually asks: what does touching this key mean.
    """

    FEATURE_FLAG = "feature-flag"
    GATE = "gate"
    #: Arms or disarms a gate. Distinct from :attr:`GATE`, which is a gate declaring what
    #: would prove it fired: a switch declares nothing, so the two are not the same key set
    #: and a switch with no ``GateEvidence`` entry read as PLAIN — an ordinary tunable — for
    #: every audit that asked this view rather than the write predicate.
    GATE_SWITCH = "gate-switch"
    SAFETY_POSTURE = "safety-posture"
    COLD = "cold"
    REGISTRY = "registry"
    RETIRED = "retired"
    PLAIN = "plain"


@dataclass(frozen=True, slots=True)
class SettingTaxon:
    """One key's governance classes, each carrying the registry payload behind it."""

    key: str
    classes: frozenset[SettingClass]
    flag: FeatureFlag | None = None
    gate: GateEvidence | None = None
    retirement: RetiredSetting | None = None


@cache
def taxonomy() -> dict[str, SettingTaxon]:
    """Every key that exists — live or retired — classified from the registries that own it."""
    retirements = {entry.key: entry for entry in RETIRED_SETTINGS}
    cold = set(COLD_SETTINGS) | set(COLD_HOOK_SETTINGS)
    return {
        key: _taxon(key, retirements.get(key), cold=key in cold)
        for key in sorted(set(ALL_KNOWN_CONFIG_SETTINGS) | set(retirements))
    }


def _taxon(key: str, retirement: RetiredSetting | None, *, cold: bool) -> SettingTaxon:
    flag = FEATURE_FLAGS.get(key)
    gate = GATE_EVIDENCE.get(key)
    claimed = {
        SettingClass.FEATURE_FLAG: flag is not None,
        SettingClass.GATE: gate is not None,
        SettingClass.GATE_SWITCH: is_gate_switch(key),
        SettingClass.SAFETY_POSTURE: key in SAFETY_POSTURE_KEYS,
        SettingClass.COLD: cold,
        SettingClass.REGISTRY: key in REGISTRY_KEYS,
        SettingClass.RETIRED: retirement is not None,
    }
    classes = {kind for kind, held in claimed.items() if held} or {SettingClass.PLAIN}
    return SettingTaxon(key, frozenset(classes), flag=flag, gate=gate, retirement=retirement)


def classify(key: str) -> SettingTaxon:
    """*key*'s governance classes, or a loud refusal naming the two domains it is absent from."""
    try:
        return taxonomy()[key]
    except KeyError:
        msg = f"{key!r} is neither a live config setting nor a recorded retirement"
        raise UnclassifiedSettingError(msg) from None


def governance_trailer(key: str) -> str:
    """The ``[gate, feature flag, …]`` trailer ``config_setting set``/``get`` appends.

    So an operator flipping a governed key sees WHAT they are flipping without a second
    lookup — a gate whose value decides whether a quality check enforces, a safety-posture
    value that may never diverge from shipped, a cold key the pre-Django hooks read outside
    the app, or a staged flag that dies with the code it gates. A plain setting, and a key
    this view cannot place, both carry nothing: the trailer decorates a value the caller has
    already resolved, so it must never turn a working read into a crash.
    """
    taxon = taxonomy().get(key)
    if taxon is None or taxon.classes == {SettingClass.PLAIN}:
        return ""
    parts = [kind.value for kind in SettingClass if kind in taxon.classes]
    if taxon.flag is not None:
        parts.append(f"stage={taxon.flag.stage.value}, tracking {taxon.flag.tracking_issue}")
    return f"[{', '.join(parts)}]"


def owner_only_reason(key: str) -> str:
    """Why writing *key* is the owner's act rather than an unattended one (empty = writable).

    ONE answer for every surface that has to ask it: the MCP write tool refuses on it, and
    the ``ConfigSetting`` write chokepoint refuses an unattended principal on it. Six
    hand-rolled lanes on one surface is how the question comes to have two answers — the
    divergence B11 exists to remove.

    Each lane names a distinct reason a write is a decision rather than a tuning: a cold key
    the pre-Django hooks read outside the app (a leak-scrub list, the fail-open switch), a
    flag whose stage is directive- and lifecycle-governed, a registry row that redirects
    overlay code paths, a safety-posture value whose write IS an authorization, and a gate
    switch that arms or disarms a live check. Every lane reads the TAXON — a lane that
    re-derived its own class is how this predicate and :func:`classify` came to disagree
    about what a gate is.

    An unplaceable key is writable rather than a crash: the predicate guards a write the
    caller has already composed, so it must never turn an ordinary set into an exception.
    """
    taxon = taxonomy().get(key)
    classes = taxon.classes if taxon is not None else frozenset()
    lanes: tuple[tuple[bool, str], ...] = (
        # Declared per overlay, so no static taxon can exist for it.
        (is_pass_key_setting(key), "credential coordinate — picks the secret the factory authenticates with"),
        (
            SettingClass.COLD in classes,
            "cold-read key — leak-scrub list / fail-open switch / agent routing, human/CLI-only",
        ),
        (SettingClass.FEATURE_FLAG in classes, "feature flag — directive-/lifecycle-governed, human/CLI-only"),
        (SettingClass.REGISTRY in classes, "registry row — redirects overlay code paths, human/CLI-only"),
        (SettingClass.SAFETY_POSTURE in classes, "safety-posture key — its write IS an authorization; human/CLI-only"),
        (SettingClass.GATE in classes, "quality gate — arms or disarms a live check, human/CLI-only"),
        (SettingClass.GATE_SWITCH in classes, "safety-gate key — flip via the CLI, never unattended"),
    )
    return next((reason for matched, reason in lanes if matched), "")


__all__ = [
    "GATE_SWITCH_GLOBS",
    "SettingClass",
    "SettingTaxon",
    "UnclassifiedSettingError",
    "classify",
    "governance_trailer",
    "is_gate_switch",
    "owner_only_reason",
    "taxonomy",
]
