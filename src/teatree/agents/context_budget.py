"""Byte budget for the headless system-context append — the E2BIG spawn guard.

The claude-agent-sdk passes the whole assembled system context as ONE
``--append-system-prompt`` argv element (its subprocess transport). Linux caps a
single argv element at ``MAX_ARG_STRLEN`` = 128 KiB, so an oversized append makes
the ``claude`` child die at spawn with ``OSError: [Errno 7] Argument list too
long``. :data:`MAX_APPEND_BYTES` bounds the append well under that limit, leaving
headroom for the rest of argv; :func:`enforce_budget` truncates the largest
budgetable blocks first and leaves a pointer marker so the agent knows context
was elided rather than silently dropped.

Every production skill bundle is 1.7-2.3x the budget, so truncation is the normal
path, not the exception. It therefore drops whole ``## `` sections off the tail
and names them, rather than keeping a byte prefix: a byte cut lands mid-sentence
at an arbitrary offset and leaves the agent unable to tell which rule it lost,
and this lane has no Skill tool to re-read the missing body by reference.

The bound is a postcondition, not an accounting result. Truncation is by exact
substring replace, so a block the assembled context never embedded reclaims zero
bytes however large it is; the pass credits a reduction only once the replace has
landed, and re-measures the finished text rather than trusting the running
overage. The kernel limit the budget protects is ``MAX_ARG_STRLEN`` — a cap on
any SINGLE argument (measured on Linux: 131,071 accepted, 131,072 refused) — not
the multi-megabyte total ``ARG_MAX``, so a total-size check never sees this.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import accumulate

logger = logging.getLogger(__name__)

# 96 KiB — comfortably below the 128 KiB kernel per-argv-element limit, leaving
# ~32 KiB of headroom for the preset prefix and the rest of the spawn argv.
MAX_APPEND_BYTES = 96 * 1024

#: Caps the marker so a bundle shedding hundreds of sections cannot spend kilobytes
#: of the budget listing them; the rest collapse into a count.
MAX_NAMED_DROPPED_SECTIONS = 20

_SKILL_HEADER_RE = re.compile(r"^--- SKILL: (?P<name>.+?) ---$")
_SECTION_HEADING_RE = re.compile(r"^## +(?P<title>.+?)\s*$")

#: Cited by the backstop cut, which happens after every budgetable block has
#: already been truncated — no single block is to blame, so it names the append.
_OVERRUN_POINTER = "the tail of this system context — no budgetable block could absorb the overage"


@dataclass(frozen=True)
class _Section:
    """One ``## ``-delimited span of a block, labelled for the elision marker."""

    label: str
    text: str


def _marker(dropped_bytes: int, where: str) -> str:
    return f"\n[…truncated {dropped_bytes} bytes; see {where}]"


def _section_marker(dropped_bytes: int, labels: Sequence[str], where: str) -> str:
    named = list(labels[:MAX_NAMED_DROPPED_SECTIONS])
    remaining = len(labels) - len(named)
    listed = "; ".join(named) + (f"; +{remaining} more" if remaining else "")
    return f"\n[…truncated {dropped_bytes} bytes — dropped whole sections: {listed}. See {where}]"


def _split_sections(block: str) -> tuple[list[str], list[_Section]]:
    """Split *block* into its pre-heading preamble and its ``## `` sections.

    The preamble (0 or 1 entries, absent when the block opens on a heading) is not
    droppable, so a skill's ``--- SKILL: <name> ---`` header survives even when
    every one of its sections goes. Sections carry a ``<skill> § <heading>`` label
    when those headers are present, so a marker naming a dropped section also says
    which skill lost it. Rejoining the preamble and every section with a newline
    reproduces *block* byte for byte.
    """
    labels: list[str] = [""]
    bodies: list[list[str]] = [[]]
    skill = ""
    for line in block.split("\n"):
        if header := _SKILL_HEADER_RE.match(line):
            skill = header["name"]
        if heading := _SECTION_HEADING_RE.match(line):
            title = heading["title"]
            labels.append(f"{skill} § {title}" if skill else title)
            bodies.append([line])
        else:
            bodies[-1].append(line)
    preamble = ["\n".join(bodies[0])] if bodies[0] else []
    sections = [_Section(label, "\n".join(body)) for label, body in zip(labels[1:], bodies[1:], strict=True)]
    return preamble, sections


def _truncate_sections(block: str, keep_bytes: int, *, where: str) -> str | None:
    """Drop whole ``## `` sections off the tail of *block* to fit *keep_bytes*.

    ``None`` when the block carries no droppable section, or when even keeping
    none of them overruns — the caller falls back to the byte prefix so the
    argv-element bound holds unconditionally. Keeps as many sections as fit, so
    the elision is the smallest section-aligned one that clears the budget.
    """
    preamble, sections = _split_sections(block)
    if not sections:
        return None
    part_bytes = [len(p.encode()) for p in preamble] + [len(s.text.encode()) for s in sections]
    cumulative = list(accumulate(part_bytes, initial=0))
    total = len(block.encode())
    labels = [s.label for s in sections]

    def rendered_bytes(kept_sections: int) -> int:
        parts = len(preamble) + kept_sections
        return cumulative[parts] + max(0, parts - 1)  # the "\n" joining each part

    for keep in range(len(sections) - 1, -1, -1):
        kept_bytes = rendered_bytes(keep)
        marker = _section_marker(total - kept_bytes, labels[keep:], where)
        if kept_bytes + len(marker.encode()) <= keep_bytes:
            return "\n".join([*preamble, *(s.text for s in sections[:keep])]) + marker
    return None


def _truncate_bytes(block: str, keep_bytes: int, *, where: str) -> str:
    """Return *block* cut to a byte prefix of at most *keep_bytes* plus a pointer marker.

    The fallback for a block with no ``## `` section to drop (a JSON survey, a
    prose summary) or whose preamble alone overruns. The marker's byte count is
    sized against the worst case (the whole block dropped) so a shorter actual
    drop only shrinks the marker — the result never exceeds *keep_bytes*. The kept
    prefix is decoded with ``errors="ignore"`` so a byte-slice never splits a
    multibyte codepoint.
    """
    encoded = block.encode()
    worst_case_marker = _marker(len(encoded), where)
    content_budget = max(0, keep_bytes - len(worst_case_marker.encode()))
    kept = encoded[:content_budget].decode(errors="ignore")
    dropped = len(encoded) - len(kept.encode())
    return f"{kept}{_marker(dropped, where)}"


def _truncate_block(block: str, keep_bytes: int, *, where: str) -> str:
    """Return *block* shortened to at most *keep_bytes* UTF-8 bytes plus a pointer marker."""
    if keep_bytes >= len(block.encode()):
        return block
    sectioned = _truncate_sections(block, keep_bytes, where=where)
    return sectioned if sectioned is not None else _truncate_bytes(block, keep_bytes, where=where)


def _bounded(text: str, *, max_bytes: int) -> str:
    """Return *text* if it fits *max_bytes*, else cut it to fit and say so loudly.

    The pass's postcondition, asserted against the byte count rather than trusted
    from the reclaim arithmetic. Cutting rather than raising keeps the dispatch
    alive on a degraded context: the whole point of the budget is that the spawn
    survives, and an exception here kills the same work E2BIG would. The ``ERROR``
    names the shortfall at the point of cause, so an under-reclaiming block list
    surfaces here and not only as ``[Errno 7] Argument list too long`` from a
    subprocess several frames away.
    """
    overrun = len(text.encode()) - max_bytes
    if overrun <= 0:
        return text
    logger.error(
        "context budget overrun: %d bytes over the %d-byte append limit after truncating every budgetable "
        "block — cutting the append to fit. A block that is not a substring of the context reclaims nothing.",
        overrun,
        max_bytes,
    )
    return _truncate_bytes(text, max_bytes, where=_OVERRUN_POINTER)


def enforce_budget(text: str, blocks: Iterable[tuple[str, str]], *, max_bytes: int = MAX_APPEND_BYTES) -> str:
    """Bound *text* to *max_bytes*, truncating *blocks* in the given priority order.

    *blocks* is an ordered iterable of ``(block_text, where)`` pairs — each
    ``block_text`` is meant to be an exact substring of *text*, and ``where``
    names where the elided content still lives (the pointer the marker cites).
    Earlier blocks are truncated first, so pass them least-load-bearing first.
    Returns *text* unchanged (byte-identical) when it already fits, so a
    normal-sized context is never rewritten. A section-aligned cut reclaims at
    least the overage and usually more (it sheds whole sections), which only
    leaves later blocks intact.

    The pass is fail-CLOSED on a block it cannot find. A block the assembled
    context does not embed is a phantom: the substring replace matches nothing,
    so it reclaims zero bytes however large it is. Crediting it would drive
    ``overage`` to 0 without shrinking a single byte and exit before the block
    that is really over budget, so the reduction counts only once the replace has
    landed, and :func:`_bounded` re-checks the byte count rather than trusting
    the arithmetic. Both guards are unconditional, so a phantom introduced by a
    future caller is a no-op instead of a silently spent budget.
    """
    overage = len(text.encode()) - max_bytes
    if overage <= 0:
        return text
    for block, where in blocks:
        if overage <= 0 or not block:
            continue
        block_bytes = len(block.encode())
        truncated = _truncate_block(block, block_bytes - overage, where=where)
        reclaimed = block_bytes - len(truncated.encode())
        if reclaimed <= 0:
            continue
        replaced = text.replace(block, truncated, 1)
        if replaced == text:  # the block is not in *text* — nothing was reclaimed
            continue
        text = replaced
        overage -= reclaimed
    return _bounded(text, max_bytes=max_bytes)
