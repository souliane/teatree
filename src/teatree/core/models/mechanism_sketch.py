"""The typed, ratifiable design contract a directive interpreter produces (PR-6).

A :class:`MechanismSketch` is the structured form of a plain-language directive —
the constraint reduced to (setting key, neutral default, core policy chokepoint,
overlay activation, acceptance tests, REJECTED alternatives). It is the artifact
the human ratifies ONCE, cheaply, before any code exists; every later stage
conforms to it. The frozen dataclass is the in-memory shape; :meth:`to_dict` /
:meth:`from_dict` are the JSON round-trip stored on ``Directive.mechanism_sketch``.

:func:`validate_sketch_structure` is the DETERMINISTIC structural half of the
recorder gate that runs server-side before the sketch is written (the maker≠checker
half): a sketch is recorded only if its named core chokepoint really exists AND is a
core seam (not an overlay-local patch), its setting key is a real identifier, and —
the N=2 litmus, recorded — it names at least one rejected alternative. The activation-
scope registry check is the recorder gate's (it needs the overlay registry the pure
model layer must not import). An interpreter that hands back a hack has to name and
reject the hack in writing before the human ever sees it; an envelope that fails
validation fails the interpret task, so the loop redispatches rather than record garbage.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


class MechanismSketchDict(TypedDict, total=False):
    """The JSON wire shape of a :class:`MechanismSketch` — what an interpreter emits.

    Canonical here (the model layer); ``teatree.agents.result_schema`` imports it for
    the ``directive_interpretation`` envelope. All keys optional because the recorder
    validates a possibly-malformed hand-back before it becomes a sketch.
    """

    kind: str
    setting_key: str
    setting_type: str
    neutral_default: object
    policy_chokepoint: str
    activation_scope: str
    activation_value: object
    rejected_alternatives: list[str]
    acceptance_tests: list[str]
    refactors: list[str]
    behavior_probe: str
    probe_none_reason: str


#: The day-one mechanism-kind catalog (§3.2). ``setting_policy_gate`` builds a
#: core setting + a policy check at the seam and activates the overlay;
#: ``activation_only`` is the duplication-check branch — the generic mechanism
#: already exists, so only the per-overlay ``ConfigSetting`` row is applied. The
#: catalog grows PR by PR; an uninterpretable directive is parked as a
#: clarification, never forced into a wrong kind.
KINDS_REQUIRING_ACCEPTANCE_TESTS: frozenset[str] = frozenset({"setting_policy_gate"})
MECHANISM_KINDS: frozenset[str] = frozenset({"setting_policy_gate", "activation_only"})

#: A ``policy_chokepoint`` under any of these path segments is an overlay-local
#: patch, not the core seam every overlay flows through — the structural refusal
#: of the one-off hack (§4.0 step 2). The core seam lives under ``src/teatree``.
_OVERLAY_PATH_MARKERS: tuple[str, ...] = ("/overlays/", "overlays/", "/contrib/", "contrib/")
_CORE_SEAM_ROOT = "src/teatree/"


class MechanismSketchError(ValueError):
    """Raised when an interpreter envelope cannot be recorded as a valid sketch."""


@dataclass(frozen=True, slots=True)
class MechanismSketch:
    """One directive's ratified design contract — the generic-shape decision, typed."""

    kind: str
    setting_key: str
    setting_type: str
    neutral_default: object
    policy_chokepoint: str
    activation_scope: str
    activation_value: object
    #: The N=2 litmus, recorded: the overlay-local one-off named and rejected in
    #: writing. A sketch with an empty list is incomplete — the hack was never
    #: considered, so it cannot have been ruled out.
    rejected_alternatives: tuple[str, ...]
    acceptance_tests: tuple[str, ...] = ()
    refactors: tuple[str, ...] = ()
    behavior_probe: str = ""
    probe_none_reason: str = ""

    def to_dict(self) -> MechanismSketchDict:
        """The JSON-serialisable form stored on ``Directive.mechanism_sketch``."""
        return MechanismSketchDict(
            kind=self.kind,
            setting_key=self.setting_key,
            setting_type=self.setting_type,
            neutral_default=self.neutral_default,
            policy_chokepoint=self.policy_chokepoint,
            activation_scope=self.activation_scope,
            activation_value=self.activation_value,
            rejected_alternatives=list(self.rejected_alternatives),
            acceptance_tests=list(self.acceptance_tests),
            refactors=list(self.refactors),
            behavior_probe=self.behavior_probe,
            probe_none_reason=self.probe_none_reason,
        )

    @classmethod
    def from_dict(cls, raw: MechanismSketchDict) -> "MechanismSketch":
        """Rebuild a sketch from its stored JSON (the inverse of :meth:`to_dict`)."""
        return cls(
            kind=str(raw.get("kind", "")),
            setting_key=str(raw.get("setting_key", "")),
            setting_type=str(raw.get("setting_type", "")),
            neutral_default=raw.get("neutral_default"),
            policy_chokepoint=str(raw.get("policy_chokepoint", "")),
            activation_scope=str(raw.get("activation_scope", "")),
            activation_value=raw.get("activation_value"),
            rejected_alternatives=_str_tuple(raw.get("rejected_alternatives")),
            acceptance_tests=_str_tuple(raw.get("acceptance_tests")),
            refactors=_str_tuple(raw.get("refactors")),
            behavior_probe=str(raw.get("behavior_probe", "")),
            probe_none_reason=str(raw.get("probe_none_reason", "")),
        )


def _str_tuple(value: object) -> tuple[str, ...]:
    """Normalise a JSON list (or lone string) to a tuple of non-blank strings."""
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def chokepoint_path(policy_chokepoint: str) -> str:
    """The repo-relative file path of a ``path::symbol`` chokepoint reference."""
    return policy_chokepoint.split("::", 1)[0].strip()


def _validate_chokepoint(policy_chokepoint: str) -> str | None:
    path = chokepoint_path(policy_chokepoint)
    if not path:
        return "policy_chokepoint is required (a `src/teatree/...::symbol` core seam)"
    normalized = path.replace("\\", "/")
    if not normalized.startswith(_CORE_SEAM_ROOT) or any(marker in normalized for marker in _OVERLAY_PATH_MARKERS):
        return (
            f"policy_chokepoint {path!r} is not a core seam: the constraint must live at a "
            f"{_CORE_SEAM_ROOT}... chokepoint every overlay flows through, never an overlay-local patch"
        )
    if not (_repo_root() / normalized).is_file():
        return f"policy_chokepoint file {path!r} does not exist at HEAD"
    return None


def _check_kind(raw: MechanismSketchDict) -> str | None:
    kind = str(raw.get("kind", "")).strip()
    return None if kind in MECHANISM_KINDS else f"kind {kind!r} not in the catalog {sorted(MECHANISM_KINDS)}"


def _check_setting_key(raw: MechanismSketchDict) -> str | None:
    setting_key = str(raw.get("setting_key", "")).strip()
    return None if setting_key.isidentifier() else f"setting_key {setting_key!r} is not a valid identifier"


def _check_rejected_alternatives(raw: MechanismSketchDict) -> str | None:
    if _str_tuple(raw.get("rejected_alternatives")):
        return None
    return "rejected_alternatives is empty: the sketch must name and reject the overlay-local one-off (N=2 litmus)"


def _check_acceptance_tests(raw: MechanismSketchDict) -> str | None:
    kind = str(raw.get("kind", "")).strip()
    if kind in KINDS_REQUIRING_ACCEPTANCE_TESTS and not _str_tuple(raw.get("acceptance_tests")):
        return f"acceptance_tests is empty: a {kind!r} sketch must name the tests that prove the mechanism"
    return None


def _check_chokepoint(raw: MechanismSketchDict) -> str | None:
    return _validate_chokepoint(str(raw.get("policy_chokepoint", "")))


#: The STRUCTURAL recorder checks (§3.4), applied in order — the first finding wins.
#: kind in catalog; setting_key a real identifier; a core (never overlay/contrib) seam
#: that exists at HEAD; the N=2-litmus rejected alternative recorded; and, for a
#: mechanism-building kind, the acceptance tests named. The activation-scope registry
#: check is the recorder's (``directive_interpret_gate``) — it needs the overlay
#: registry, which the pure model layer must not import.
_STRUCTURE_CHECKS: tuple[Callable[[MechanismSketchDict], str | None], ...] = (
    _check_kind,
    _check_setting_key,
    _check_chokepoint,
    _check_rejected_alternatives,
    _check_acceptance_tests,
)


def validate_sketch_structure(raw: MechanismSketchDict) -> str | None:
    """Return the first STRUCTURAL finding if *raw* is not a recordable sketch, else ``None``.

    Each check is fail-loud with a named reason so a rejected sketch tells the
    interpreter exactly what to fix (see :data:`_STRUCTURE_CHECKS`). The
    activation-scope registry check is layered on by the recorder gate.
    """
    for check in _STRUCTURE_CHECKS:
        finding = check(raw)
        if finding is not None:
            return finding
    return None


def sketch_from_envelope(raw: MechanismSketchDict) -> MechanismSketch:
    """Validate structure then build a :class:`MechanismSketch`, raising on any finding.

    The single structural writer path: :func:`validate_sketch_structure` first (so a
    structurally-invalid envelope never becomes a sketch), then
    :meth:`MechanismSketch.from_dict`. The recorder gate applies the overlay-scope
    registry check separately, before this.
    """
    finding = validate_sketch_structure(raw)
    if finding is not None:
        raise MechanismSketchError(finding)
    return MechanismSketch.from_dict(raw)
