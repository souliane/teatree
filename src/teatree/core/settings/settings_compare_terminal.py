"""The settings comparison rendered as terminal text — the Compare Instances page in a shell.

**The comparison is not built here.** The instance band, the comparability verdict and every
row come from :func:`~teatree.core.settings.settings_compare.build_compare_view` — the identical call
:func:`~teatree.dash.views.settings.settings_compare` makes for the page. This module only
decides how that one view reads in a terminal, so the two surfaces cannot drift into two
answers: a change to what "differs" means lands on both, or on neither.

Three things the page says with layout that plain text loses unless it says them outright:

*   **An instance that did not answer is NAMED, with its reason.** A configured box quietly
    dropped from the output reads as agreement between the boxes that did answer, which is the
    one wrong answer this surface must never give. A peer with no teatree deployed on it can
    never answer — that is a permanent non-column, and reporting it as a named non-answer is
    the honest result. An error, or a silent omission, both misreport it.
*   **The comparability verdict comes BEFORE the rows, not after.** Where the boxes declare
    different settings, the rows below cannot be read as configuration drift at all. A diff
    printed without that caveat is LESS truthful than no diff, so the caveat is printed first
    and repeated over the difference table it qualifies.
*   **Differences are grouped, values first.** ``build_compare_view`` already sorts by
    :attr:`~teatree.core.settings.settings_compare.CompareRow.rank`; a terminal adds the heading and the
    count per kind, so a hundred rows answer "what differs, and does it matter?" by being
    scanned once from the top rather than read in full. ``code-version`` rows are a difference
    in what the code DECLARES, never in configuration — last, and labelled as such.
"""

import json
import textwrap
from collections.abc import Iterable, Sequence
from typing import Any, Final

from teatree.core.settings.settings_compare import CompareRow, CompareView, RowKind
from teatree.core.settings.settings_compat import CompatRow
from teatree.core.settings.settings_files import LoadRefusal
from teatree.core.settings.settings_peers import PeerSnapshot

#: ``--kind`` spellings. A terminal option cannot carry ``RowKind``'s own spaced values, and a
#: slug is what an operator can type; the mapping lives here so the enum stays the one truth.
KIND_SLUGS: Final[dict[str, RowKind]] = {
    "values": RowKind.VALUES,
    "override": RowKind.OVERRIDE,
    "code": RowKind.CODE,
}

#: The order the difference groups are printed in — the page's rank order, named.
_GROUP_ORDER: Final[tuple[RowKind, ...]] = (RowKind.VALUES, RowKind.OVERRIDE, RowKind.CODE)

#: What each group means, so the heading answers "does it matter?" without a second document.
_GROUP_NOTE: Final[dict[RowKind, str]] = {
    RowKind.VALUES: "both boxes hold a value and the values disagree",
    RowKind.OVERRIDE: "one box holds a value where the other holds no row at all",
    RowKind.CODE: "a difference in what the code declares, not in configuration",
}

_MIN_VALUE_WIDTH: Final = 14
_FIELD_WIDTH: Final = 11
_MIN_WRAP_WIDTH: Final = 40
_SEVERITY_WIDTH: Final = 10
_MIN_READING_WIDTH: Final = 8
_NAME_WIDTH: Final = 30
_WHERE_WIDTH: Final = 26
_IMPORT_WIDTH: Final = 9
_GAP: Final = "  "
_ELLIPSIS: Final = "…"
_INDENT: Final = "  "

#: The caveat a value diff between differently-declared boxes must never be printed without.
_NOT_DRIFT: Final = (
    "The rows below are still a true reading of what each box stores — but they cannot be read as "
    "configuration drift while the boxes disagree about which settings exist."
)

#: What to say when nothing this side opens can make a peer answer — a permanent non-column.
_NO_REMEDY: Final = (
    "none from here — the forward carried the request and the far end answered THAT, so no tunnel "
    "this side opens changes it. Wait longer with --timeout if the box is merely slow; compare a box "
    "that serves no snapshot at all from a record instead, with --snapshot <file>"
)


def render_compare(
    view: CompareView,
    *,
    width: int = 100,
    kinds: Sequence[RowKind] = (),
    full: bool = False,
    refusals: Sequence[LoadRefusal] = (),
) -> str:
    """*view* as terminal text: who answered, whether they may be compared, and what differs."""
    blocks = [
        _instances_block(view.instances, width=width),
        _refusals_block(refusals) if refusals else "",
        _comparability_block(view, width=width),
        _differences_block(view, width=width, kinds=kinds, full=full),
    ]
    return "\n\n".join(block for block in blocks if block)


def compare_payload(view: CompareView, *, refusals: Sequence[LoadRefusal] = ()) -> dict[str, Any]:
    """The same view as JSON — every instance, the verdict, and every row, nothing summarised away."""
    return {
        "instances": [_instance_payload(instance) for instance in view.instances],
        "answered": list(view.labels),
        "comparable": view.compat.comparable if view.compat else None,
        "verdict": view.compat.verdict if view.compat else "",
        "blocking": [_signal_payload(row) for row in (view.compat.blocking if view.compat else ())],
        "warnings": [_signal_payload(row) for row in (view.compat.warnings if view.compat else ())],
        "total_rows": view.total_rows,
        "truncated": view.truncated,
        "error": view.error,
        "refusals": [{"source": refusal.source, "reason": refusal.reason} for refusal in refusals],
        "rows": [_row_payload(row) for row in view.rows],
    }


def render_json(view: CompareView, *, refusals: Sequence[LoadRefusal] = ()) -> str:
    return json.dumps(compare_payload(view, refusals=refusals), indent=2, sort_keys=False)


# ── the instance band ────────────────────────────────────────────────────────────────────


def _instances_block(instances: Sequence[PeerSnapshot], *, width: int) -> str:
    """Every CONFIGURED instance, answered or not — never only the ones that answered."""
    lines = ["Instances"]
    for instance in instances:
        lines.append(f"{_INDENT}{instance.label}{f'  ({instance.note})' if instance.note else ''}")
        for label, text in _instance_fields(instance):
            lines.extend(f"{_INDENT * 2}{line}" for line in _field(label, text, width=width))
    return "\n".join(lines)


def _instance_fields(instance: PeerSnapshot) -> list[tuple[str, str]]:
    """What this instance contributed — and, when that is nothing, why, and what would help."""
    fields = [("source", instance.provenance)]
    if instance.reachable:
        state = "loaded" if instance.from_file else "answered"
        fields.append(("state", f"{state}, captured {instance.captured_at or 'an unstated time'}"))
        return fields
    fields.append(("state", f"no answer — {_one_line(instance.error)}"))
    # `_forward_is_down` withheld the tunnel advice, so the far end itself answered this.
    remedy = f"bring its tunnel up: {instance.tunnel_command}" if instance.tunnel_command else _NO_REMEDY
    fields.append(("remedy", remedy))
    return fields


def _field(label: str, text: str, *, width: int) -> list[str]:
    """*label* then *text*, hanging-indented — a long reason stays inside the band it belongs to."""
    body = textwrap.wrap(
        text, width=max(_MIN_WRAP_WIDTH, width - len(_INDENT) * 2 - _FIELD_WIDTH), break_long_words=False
    ) or [""]
    return [f"{label:<{_FIELD_WIDTH}}{body[0]}", *(f"{' ' * _FIELD_WIDTH}{line}" for line in body[1:])]


def _one_line(text: str) -> str:
    """*text* with its line breaks collapsed — an exception message must not break the band."""
    return " ".join(text.split())


def _refusals_block(refusals: Sequence[LoadRefusal]) -> str:
    return "\n".join(
        ["Snapshots not loaded", *(f"{_INDENT}{refusal.source}: {refusal.reason}" for refusal in refusals)]
    )


# ── comparability ────────────────────────────────────────────────────────────────────────


def _comparability_block(view: CompareView, *, width: int) -> str:
    """The verdict, and the signals that decided it — printed BEFORE any row it qualifies."""
    if view.compat is None:
        return ""
    lines = ["Comparability", *_paragraph(view.compat.verdict, width=width)]
    if not view.compat.comparable:
        lines.extend(_paragraph(_NOT_DRIFT, width=width))
    signals = (*view.compat.blocking, *view.compat.warnings)
    longest = max((len(row.signal.label) for row in signals), default=0)
    reading_width = _reading_width(view.compat.labels, width=width, label_width=longest)
    for row in signals:
        readings = ", ".join(
            f"{label}={_clip(reading or '—', reading_width)}"
            for label, reading in zip(view.compat.labels, row.readings, strict=True)
        )
        lines.append(f"{_INDENT}{row.severity:<{_SEVERITY_WIDTH}}{row.signal.label + ':':<{longest + 2}}{readings}")
    return "\n".join(lines)


def _reading_width(labels: Sequence[str], *, width: int, label_width: int) -> int:
    """How much of each digest fits on one signal line once every instance has been named."""
    if not labels:
        return _MIN_READING_WIDTH
    spent = len(_INDENT) + _SEVERITY_WIDTH + label_width + 2 + sum(len(label) + 3 for label in labels)
    return max(_MIN_READING_WIDTH, (width - spent) // len(labels))


# ── the differences ──────────────────────────────────────────────────────────────────────


def _differences_block(view: CompareView, *, width: int, kinds: Sequence[RowKind], full: bool) -> str:
    if view.error:
        return f"Differences\n{_INDENT}{view.error}"
    header = f"Differences ({view.total_rows})  {_tally(view.rows)}"
    lines = [header]
    if unreachable := view.unreachable:
        # Restated here rather than left to the band above: this is the caveat that applies to
        # every value printed below, and it must not be a scroll away from them.
        named = ", ".join(instance.label for instance in unreachable)
        lines.append(f"{_INDENT}Covers only the instances that answered. No reading below is from {named}.")
    if view.truncated:
        lines.append(f"{_INDENT}Showing the first {len(view.rows)} of {view.total_rows} rows.")
    if not view.rows:
        lines.append(f"{_INDENT}No differences.")
        return "\n".join(lines)
    widths = _column_widths(view.labels, width=width)
    wanted = tuple(kinds) or _GROUP_ORDER
    for kind in _GROUP_ORDER:
        group = [row for row in view.rows if row.kind is kind]
        if not group or kind not in wanted:
            continue
        lines.extend(("", f"{_INDENT}{kind} ({len(group)}) — {_GROUP_NOTE[kind]}"))
        if not full:
            lines.append(f"{_INDENT}{_header_line(view.labels, widths)}")
        rendered = (_row_block(row) if full else _row_lines(row, widths) for row in group)
        lines.extend(f"{_INDENT}{line}" for block in rendered for line in block)
    return "\n".join(lines)


def _paragraph(text: str, *, width: int) -> list[str]:
    return [
        f"{_INDENT}{line}"
        for line in textwrap.wrap(text, width=max(_MIN_WRAP_WIDTH, width - len(_INDENT)), break_long_words=False)
    ]


def _tally(rows: Sequence[CompareRow]) -> str:
    counted = [(kind, sum(1 for row in rows if row.kind is kind)) for kind in _GROUP_ORDER]
    return ", ".join(f"{count} {kind}" for kind, count in counted if count)


def _column_widths(labels: Sequence[str], *, width: int) -> tuple[int, ...]:
    """Name, where, disposition, then one width per instance — the residue split evenly."""
    fixed = (_NAME_WIDTH, _WHERE_WIDTH, _IMPORT_WIDTH)
    if not labels:
        return fixed
    spent = sum(fixed) + len(_GAP) * (len(fixed) + len(labels) - 1)
    each = max(_MIN_VALUE_WIDTH, (width - spent) // len(labels))
    return (*fixed, *(max(each, len(label)) for label in labels))


def _header_line(labels: Sequence[str], widths: Sequence[int]) -> str:
    return _join(("Row", "Where", "Imported", *labels), widths)


def _row_lines(row: CompareRow, widths: Sequence[int]) -> list[str]:
    """One row as one table line — every value clipped to its column."""
    cells = [cell.text for cell in row.cells]
    lines = [_join((row.title, row.subtitle, row.outcome.disposition, *cells), widths)]
    if row.stale_on:
        lines.append(f"{_INDENT}stored on {', '.join(row.stale_on)} but not declared there")
    return lines


def _row_block(row: CompareRow) -> list[str]:
    """One row as a block — ``--full``, where a value is never clipped and the rule is named."""
    label_width = max((len(cell.label) for cell in row.cells), default=0)
    lines = [f"{row.title}  {row.subtitle}"]
    lines.extend(f"{_INDENT}{cell.label.ljust(label_width)}  {cell.text}" for cell in row.cells)
    if row.stale_on:
        lines.append(f"{_INDENT}stored on {', '.join(row.stale_on)} but not declared there")
    lines.append(f"{_INDENT}if imported: {row.outcome.disposition} — {row.outcome.reason}")
    return lines


def _join(values: Iterable[str], widths: Sequence[int]) -> str:
    padded = [_fit(str(value), width) for value, width in zip(values, widths, strict=True)]
    return _GAP.join(padded).rstrip()


def _fit(value: str, width: int) -> str:
    return _clip(value, width).ljust(width)


def _clip(value: str, width: int) -> str:
    """*value* in at most *width* columns, the cut marked so a truncation is never silent."""
    return value if len(value) <= width else value[: width - 1] + _ELLIPSIS


# ── json ─────────────────────────────────────────────────────────────────────────────────


def _instance_payload(instance: PeerSnapshot) -> dict[str, Any]:
    return {
        "label": instance.label,
        "note": instance.note,
        "source": instance.provenance,
        "origin": str(instance.origin),
        "answered": instance.reachable,
        "captured_at": instance.captured_at,
        "error": instance.error,
        "tunnel_command": instance.tunnel_command,
    }


def _signal_payload(row: CompatRow) -> dict[str, Any]:
    return {
        "signal": row.signal.label,
        "severity": str(row.severity),
        "note": row.signal.note,
        "readings": list(row.readings),
    }


def _row_payload(row: CompareRow) -> dict[str, Any]:
    return {
        "kind": str(row.kind),
        "surface": row.surface,
        "scope": row.scope,
        "key": row.title,
        "where": row.subtitle,
        "disposition": str(row.outcome.disposition),
        "rule": row.outcome.rule,
        "reason": row.outcome.reason,
        "stale_on": list(row.stale_on),
        "cells": [
            {"label": cell.label, "present": cell.present, "declared": cell.known, "value": cell.value}
            for cell in row.cells
        ],
    }


__all__ = ["KIND_SLUGS", "compare_payload", "render_compare", "render_json"]
