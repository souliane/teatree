"""Render a django-fsm model's transitions as Mermaid and splice into Markdown.

Pure string transforms only — no filesystem or git I/O. The generate/check
hooks (``scripts/hooks/generate_fsm_diagrams.py``,
``scripts/hooks/check_fsm_diagrams_sync.py``) own the I/O and reuse these so
every consumer of a diagram stays byte-identical to the model and drift-gated,
mirroring the cli-reference pipeline.
"""

from typing import TYPE_CHECKING, cast

from django.db.models import Model

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_fsm import FSMField

_WILDCARD = "*"


class MarkerNotFoundError(ValueError):
    """A Markdown consumer is missing its BEGIN/END generation markers."""


def render_fsm_mermaid(model: type[Model], *, field: str = "state", title: str | None = None) -> str:
    """A Mermaid ``stateDiagram-v2`` for ``model``'s django-fsm transitions.

    The field's ``default`` is emitted as the ``[*] --> <default>`` entry edge.
    Every transition the FSM field registers is emitted; a ``source="*"``
    transition expands to one edge per declared state. Edges sort by
    (source ordinal, target ordinal, transition name) — the declaration order
    of the field's choices is the ordinal — so the output is byte-stable.
    """
    fsm_field = cast("FSMField", model._meta.get_field(field))  # noqa: SLF001  # Django's documented Model._meta API
    choices = cast("Iterable[tuple[object, object]]", fsm_field.choices)
    states = [str(value) for value, _label in choices]
    ordinal = {state: index for index, state in enumerate(states)}

    edges: list[tuple[int, int, str]] = []
    for transition in fsm_field.get_all_transitions(model):
        target = _state_value(transition.target)
        sources = states if transition.source == _WILDCARD else [_state_value(transition.source)]
        edges.extend((ordinal[source], ordinal[target], transition.name) for source in sources)
    edges.sort()

    lines = ["stateDiagram-v2"]
    default = _state_value(fsm_field.default)
    if default in ordinal:
        lines.append(f"    [*] --> {default}")
    lines += [f"    {states[source]} --> {states[target]} : {name}" for source, target, name in edges]
    body = "\n".join(lines)
    return f"---\ntitle: {title}\n---\n{body}" if title else body


def _state_value(state: object) -> str:
    return str(getattr(state, "value", state))


def fenced_mermaid(diagram: str) -> str:
    return f"```mermaid\n{diagram}\n```"


def inject_between_markers(text: str, *, begin: str, end: str, block: str) -> str:
    """``text`` with everything between ``begin`` and ``end`` replaced by ``block``."""
    _require_markers(text, begin=begin, end=end)
    before = text[: text.index(begin) + len(begin)]
    after = text[text.index(end) :]
    return f"{before}\n{block}\n{after}"


def extract_between_markers(text: str, *, begin: str, end: str) -> str:
    """The block currently sitting between ``begin`` and ``end`` in ``text``."""
    _require_markers(text, begin=begin, end=end)
    return text[text.index(begin) + len(begin) : text.index(end)].strip("\n")


def _require_markers(text: str, *, begin: str, end: str) -> None:
    if begin not in text or end not in text:
        msg = f"missing generation markers ({begin} / {end})"
        raise MarkerNotFoundError(msg)
