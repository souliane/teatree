"""``model@effort`` variant parsing for the matrix and benchmark lanes.

A variant is a model plus an optional reasoning-effort level, spelled
``claude-opus-4-8@xhigh`` (or a plain model name for the default effort). The
rendered :attr:`ModelVariant.tag` is the variant's identity string: it flows
unchanged through the existing per-model machinery (``MatrixRow.model``, the
run-store's per-model ledger, pass-rates, regression/cost gates), so comparing
two efforts of the same model needs no schema change. The SDK runner strips the
tag back into ``(model, effort)`` when building :class:`ClaudeAgentOptions` —
``effort`` is the SDK's first-class field, rendered by its transport as the
``claude --effort <level>`` CLI flag.
"""

import dataclasses
from typing import cast

from claude_agent_sdk.types import EffortLevel

#: The effort levels the claude CLI accepts (``claude --effort <level>``),
#: mirrored by the SDK's :data:`claude_agent_sdk.types.EffortLevel` literal.
EFFORT_LEVELS: tuple[EffortLevel, ...] = ("low", "medium", "high", "xhigh", "max")


class ModelVariantError(ValueError):
    """A ``--models`` entry that cannot be parsed into a ``(model, effort)`` variant."""


@dataclasses.dataclass(frozen=True)
class ModelVariant:
    """One ``(model, effort)`` cell of a matrix/benchmark run."""

    model: str
    effort: EffortLevel | None = None

    @property
    def tag(self) -> str:
        """The variant's identity string — ``model@effort``, or the bare model."""
        return f"{self.model}@{self.effort}" if self.effort is not None else self.model


def parse_model_variant(raw: str) -> ModelVariant:
    """Parse one ``model[@effort]`` entry; reject empty models and unknown efforts."""
    model, at, effort = (part.strip() for part in raw.partition("@"))
    if not model:
        msg = f"empty model in --models entry {raw!r}"
        raise ModelVariantError(msg)
    if not at:
        return ModelVariant(model=model)
    if effort not in EFFORT_LEVELS:
        msg = f"unknown effort {effort!r} in --models entry {raw!r}; known levels: {', '.join(EFFORT_LEVELS)}"
        raise ModelVariantError(msg)
    return ModelVariant(model=model, effort=cast("EffortLevel", effort))


def parse_model_variants(raw: str) -> list[ModelVariant]:
    """Parse a comma-separated variant list, dropping blank entries."""
    return [parse_model_variant(entry) for entry in raw.split(",") if entry.strip()]
