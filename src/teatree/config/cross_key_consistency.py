"""Cross-key config consistency — reject an inconsistent coupled pair at write time (#3688).

Some config settings are COUPLED: the valid value of one depends on the current
value of another. ``agent_harness`` (Layer 1 transport) and
``agent_harness_provider`` (Layer 2 credential) are the first such pair — a
provider is valid only under certain harnesses
(:meth:`~teatree.config.AgentHarnessProvider.valid_for`). Today a write that lands
an INCONSISTENT pair (e.g. ``agent_harness_provider=openai_compatible`` under the
default ``agent_harness=claude_sdk``) is accepted silently, then makes EVERY later
dispatch fail at :func:`~teatree.agents.harness_registry.assert_provider_valid_for_harness`
— each failure burning a claim, an attempt, and a repair-halt, fleet-wide, until an
operator notices.

This module moves that rejection to WRITE time: one loud error at the config seam
instead of a fleet-wide repair-halt flood. It is the config-layer mirror of the
dispatch-time constraint — the closed-enum
:meth:`~teatree.config.AgentHarnessProvider.valid_for` set for the two built-in
harnesses. The config layer cannot import the agents-layer OPEN harness registry
(that would be a backwards dependency edge), so an overlay-registered THIRD harness
carries no closed constraint here and is left UNCONSTRAINED — its own valid-provider
set is enforced LOUD at dispatch (:class:`~teatree.agents.harness_registry.InvalidHarnessProviderError`),
exactly as before. This never blocks a valid built-in pair and never falsely blocks
an overlay backend.

The mechanism is a small extensible registry (:data:`CROSS_KEY_RULES`) keyed on the
coupled pair, so more coupled pairs (lane/provider, model/tier) route through the
same seam as they appear — a new pair is one :class:`CrossKeyRule` appended, not new
inline branching at every write site.
"""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.config.agent_enums import AgentHarness, AgentHarnessProvider, parse_harness_name

_HARNESS_KEY = "agent_harness"
_PROVIDER_KEY = "agent_harness_provider"

# The harness a headless run resolves to when ``agent_harness`` is unset — the
# ``UserSettings.agent_harness`` field default. The pair after a provider-only
# write is judged against THIS, not against "no harness": the production trigger
# was exactly a provider pinned while the harness sat at its default.
_DEFAULT_HARNESS = AgentHarness.CLAUDE_SDK.value


def _as_str(value: object) -> str | None:
    """Normalise a stored value (``StrEnum`` / ``str`` / ``None``) to ``str | None``.

    A blank string collapses to ``None`` so an empty stored row reads as "unset".
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def check_harness_provider_pair(harness: str | None, provider: str | None) -> str | None:
    """Return a rejection reason when (*harness*, *provider*) is a pair dispatch would refuse.

    The config-layer mirror of :func:`~teatree.agents.harness_registry.assert_provider_valid_for_harness`,
    resolved through the closed-enum :meth:`~teatree.config.AgentHarnessProvider.valid_for`
    for the two built-in harnesses:

    * A ``None``/blank *provider* (no explicit Layer-2 pin) always passes.
    * A *provider* whose value does not parse is left to the registry parser to
        reject on read — not this check's concern, so it passes here.
    * An unset/blank *harness* resolves to :data:`_DEFAULT_HARNESS` (the field
        default a real dispatch would use).
    * An overlay-registered (non-built-in) *harness* is UNCONSTRAINED here — its
        valid-provider set lives in the agents-layer open registry the config
        layer cannot import, and is enforced at dispatch instead.

    Returns ``None`` for a consistent pair.
    """
    if provider is None or not str(provider).strip():
        return None
    try:
        resolved_provider = AgentHarnessProvider.parse(str(provider))
    except ValueError:
        return None
    harness_name = parse_harness_name(harness) if harness else _DEFAULT_HARNESS
    try:
        built_in = AgentHarness(harness_name)
    except ValueError:
        return None
    valid = AgentHarnessProvider.valid_for(built_in)
    if resolved_provider in valid:
        return None
    allowed = ", ".join(sorted(member.value for member in valid))
    return (
        f"agent_harness_provider={resolved_provider.value!r} is not valid under "
        f"agent_harness={built_in.value!r} (valid providers: {allowed}). "
        "Set a compatible agent_harness first."
    )


@dataclass(frozen=True, slots=True)
class CrossKeyRule:
    """One coupled-key consistency constraint over an ordered set of setting *keys*.

    *check* receives the resulting values in *keys* order (the pending write's value
    for the changed key, each other key's current effective value) and returns a
    rejection reason, or ``None`` when the pair is consistent. Adding a new coupled
    pair is one :class:`CrossKeyRule` appended to :data:`CROSS_KEY_RULES`.
    """

    keys: tuple[str, ...]
    check: Callable[..., str | None]


def _harness_provider_check(harness: object, provider: object) -> str | None:
    return check_harness_provider_pair(_as_str(harness), _as_str(provider))


CROSS_KEY_RULES: tuple[CrossKeyRule, ...] = (
    CrossKeyRule(keys=(_HARNESS_KEY, _PROVIDER_KEY), check=_harness_provider_check),
)


def validate_cross_key_write(
    changed_key: str,
    changed_value: object,
    resolve_other: Callable[[str], object],
) -> str | None:
    """Return a rejection reason when writing *changed_key*=*changed_value* would land an inconsistent pair.

    *resolve_other* returns the current effective value of another key (the caller
    reads the paired key's stored value, falling through to its default). The
    RESULTING pair — the pending value for *changed_key*, the resolved value for
    each other key in the rule — is judged. A *changed_key* in no coupled pair is a
    no-op (returns ``None``), so the check never touches an unrelated write.
    """
    for rule in CROSS_KEY_RULES:
        if changed_key not in rule.keys:
            continue
        values = [changed_value if key == changed_key else resolve_other(key) for key in rule.keys]
        if reason := rule.check(*values):
            return reason
    return None
