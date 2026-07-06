"""The CONFIGURE phase — apply the ratified overlay activation, byte-identically (north-star PR-7).

The one new power the directive loop has beyond the outer loop: a single
``ConfigSetting`` write. It is bounded by ratification — :func:`apply_activation`
writes the overlay row ONLY when the activation is byte-identical to the ratified
``MechanismSketch`` (:func:`activation_conforms`), then confirms by reading it back
through the REAL resolver (``get_effective_settings``). Any drift from what the human
ratified is refused with no write; a read-back mismatch is refused too. The write is
reversible with no deploy — :func:`clear_activation` rolls it back instantly, which
is what makes the config half of a revert safe.
"""

from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting, Directive
from teatree.core.models.mechanism_sketch import MechanismSketch

_MISSING = object()


@dataclass(frozen=True, slots=True)
class Activation:
    """The concrete overlay-config write a directive's CONFIGURING step applies."""

    setting_key: str
    value: object
    scope: str

    @classmethod
    def from_sketch(cls, sketch: MechanismSketch) -> "Activation":
        return cls(setting_key=sketch.setting_key, value=sketch.activation_value, scope=sketch.activation_scope)


@dataclass(frozen=True, slots=True)
class ConfigureResult:
    """Whether the activation was applied, plus the refusal/confirmation reason."""

    applied: bool
    reason: str


def activation_conforms(activation: Activation, sketch: MechanismSketch) -> bool:
    """Whether *activation* is byte-identical to the ratified *sketch*'s activation.

    The drift guard: the loop must write EXACTLY the key/value/scope the human
    ratified, never anything derived elsewhere. An activation that diverges on any
    of the three is refused before any write.
    """
    return (
        activation.setting_key == sketch.setting_key
        and activation.value == sketch.activation_value
        and activation.scope == sketch.activation_scope
    )


def apply_activation(directive: Directive, *, activation: Activation | None = None) -> ConfigureResult:
    """Apply the ratified overlay ``ConfigSetting`` — only if byte-identical to the sketch.

    Builds the intended activation from the ratified sketch (the *activation* arg is
    the test/override seam), refuses on drift or an empty scope, writes the overlay
    row, then reads it back through ``get_effective_settings`` to confirm it took.
    """
    sketch = directive.sketch
    if sketch is None:
        return ConfigureResult(applied=False, reason="no ratified sketch — cannot configure")
    resolved = activation if activation is not None else Activation.from_sketch(sketch)
    if not activation_conforms(resolved, sketch):
        return ConfigureResult(applied=False, reason="activation drifted from the ratified sketch — refused")
    if not resolved.scope:
        # A global (empty-scope) mechanism IS the merged core change — there is no
        # per-overlay ConfigSetting row to write. Configure is a no-op success (the
        # interpret gate blesses an empty scope as a valid global mechanism), so the
        # directive advances to VERIFYING rather than parking.
        return ConfigureResult(applied=True, reason="global mechanism — no per-overlay activation needed")
    ConfigSetting.objects.set_value(resolved.setting_key, resolved.value, scope=resolved.scope)
    effective = getattr(get_effective_settings(resolved.scope), resolved.setting_key, _MISSING)
    if effective != resolved.value:
        ConfigSetting.objects.clear(resolved.setting_key, scope=resolved.scope)
        return ConfigureResult(
            applied=False, reason=f"read-back mismatch: {resolved.setting_key}={effective!r} != {resolved.value!r}"
        )
    reason = f"activated {resolved.setting_key}={resolved.value!r} for {resolved.scope}"
    return ConfigureResult(applied=True, reason=reason)


def clear_activation(directive: Directive) -> bool:
    """Roll back the overlay activation instantly (the reversible half of a revert).

    Clears the ratified sketch's ``ConfigSetting`` row so the setting falls back to
    its neutral default; returns whether a row was removed. Idempotent — a clear when
    nothing is set is a harmless no-op.
    """
    sketch = directive.sketch
    if sketch is None or not sketch.activation_scope:
        return False
    return ConfigSetting.objects.clear(sketch.setting_key, scope=sketch.activation_scope)
