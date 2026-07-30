"""Load-time signature conformance for overlay extension points (#3526).

An overlay overrides facet hooks (``OverlayMetadata.validate_pr``,
``OverlayReview.visual_qa_targets`` …). The framework always calls each hook with
the parameters the *base* declares. An override that cannot receive one of those —
a dropped keyword-only parameter, or a dropped positional slot with no ``*args`` /
``**kwargs`` to absorb it — is call-incompatible: it works until the framework
forwards the missing argument, then raises ``TypeError`` on whichever path happens
to pass it. Conditional-forwarding a keyword "only when it deviates from the
default" hides exactly this until the non-default path runs.

Checking overrides at discovery time turns that latent, intermittent crash into a
loud registration-time error naming the overlay, the hook, and the missing
parameter — so a non-conforming overlay fails on the first call, not the unlucky
one. A renamed positional parameter (``url`` → ``issue_url``) stays legal: the
framework forwards leading arguments positionally, so only a dropped *slot* or a
dropped *keyword-only* name is a real break.
"""

import inspect
import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

_POSITIONAL = frozenset({inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD})
_KEYWORDABLE = frozenset({inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY})
_VARIADIC = frozenset({inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD})

#: Facet attribute → base class whose declared hook signatures the override must honour.
#: ``config`` is a pydantic data model, not a call-forwarded hook surface, so it is
#: out of scope here; the six behavioural facets and ``OverlayBase`` itself carry the
#: methods the framework invokes with base-declared arguments.
_SCANNED_FACETS = ("metadata", "provisioning", "runtime", "e2e", "review", "connectors")


def _missing_params(base: inspect.Signature, override: inspect.Signature) -> list[str]:
    o_params = list(override.parameters.values())
    o_positional_slots = sum(1 for p in o_params if p.kind in _POSITIONAL)
    o_has_var_positional = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in o_params)
    o_has_var_keyword = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in o_params)
    o_keyword_names = {p.name for p in o_params if p.kind in _KEYWORDABLE}

    missing: list[str] = []
    position = 0
    for param in base.parameters.values():
        if param.kind in _VARIADIC:
            continue
        if param.kind in _POSITIONAL:
            positional_ok = position < o_positional_slots or o_has_var_positional
            keyword_ok = param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and (
                param.name in o_keyword_names or o_has_var_keyword
            )
            if not (positional_ok or keyword_ok):
                missing.append(param.name)
            position += 1
        elif not (param.name in o_keyword_names or o_has_var_keyword):
            missing.append(param.name)
    return missing


def _facet_violations(label: str, base_cls: type, override_cls: type) -> list[str]:
    violations: list[str] = []
    for hook_name, base_attr in vars(base_cls).items():
        if hook_name.startswith("_") or not callable(base_attr):
            continue
        override_attr = getattr(override_cls, hook_name, None)
        if override_attr is None or override_attr is getattr(base_cls, hook_name):
            continue
        missing = _missing_params(inspect.signature(base_attr), inspect.signature(override_attr))
        if missing:
            params = ", ".join(missing)
            violations.append(
                f"{label}.{hook_name}() cannot accept parameter(s) {params} declared by "
                f"{base_cls.__name__}.{hook_name}()"
            )
    return violations


def overlay_signature_violations(overlay: "OverlayBase", *, name: str = "") -> list[str]:
    """Return one message per hook whose override cannot honour the base signature.

    Empty means the overlay conforms. Each message names the overlay, the facet
    hook, and the parameter(s) the override cannot receive.
    """
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415 — deferred: avoid an import cycle at module load

    label = name or type(overlay).__name__
    targets: list[tuple[str, type, type]] = [("OverlayBase", OverlayBase, type(overlay))]
    targets.extend((attr, type(getattr(OverlayBase, attr)), type(getattr(overlay, attr))) for attr in _SCANNED_FACETS)

    return [
        f"overlay {label!r}: {problem}"
        for facet_label, base_cls, override_cls in targets
        for problem in _facet_violations(facet_label, base_cls, override_cls)
    ]


def conforming_or_raise(overlay: "OverlayBase", name: str) -> "OverlayBase":
    """Return the overlay if it conforms; else raise naming the missing parameter(s).

    The loud path for an entry-point overlay — a signature mismatch is a
    programming bug that must fail registration, not be silently skipped.
    """
    if violations := overlay_signature_violations(overlay, name=name):
        raise ImproperlyConfigured("; ".join(violations))
    return overlay


def conforming_or_none(overlay: "OverlayBase", name: str) -> "OverlayBase | None":
    """Return the overlay if it conforms; else warn and return None.

    The lenient path for a registry-configured overlay — mirrors how a
    non-subclass registry overlay is warned and skipped rather than fatal.
    """
    if violations := overlay_signature_violations(overlay, name=name):
        logger.warning("overlay %r has non-conforming extension points: %s", name, "; ".join(violations))
        return None
    return overlay
