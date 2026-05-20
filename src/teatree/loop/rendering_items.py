"""Canonical statusline item shape and ref dataclasses (#1015).

Split out of :mod:`teatree.loop.rendering` so the line-builder module owns
classification + zone composition only. This module owns the **item shape**:
the small, reusable primitives that render every state-line item in the
canonical ``#N (short desc) (!M1, !M2)`` form, plus the small frozen
dataclasses the renderer carries around as classified refs.

Keeping the item shape here means any future row family (a new disposition
kind, a different action zone) reuses ``_render_canonical_item`` and inherits
the same look without each render path re-implementing description truncation
or comma joining.
"""

from dataclasses import dataclass, field
from typing import Protocol


class _LinkFn(Protocol):
    def __call__(self, text: str, url: object, *, colorize: bool) -> str: ...


# Canonical item-shape description width (#1015): ``#N (short desc) (!M)``.
# 40 chars is wide enough for a useful title chunk on a single-line
# statusline but narrow enough that three tickets per state line still
# fit a typical terminal. Truncated descriptions are tail-elided with a
# single-codepoint Unicode ellipsis so the visible width stays predictable.
_ITEM_DESC_LEN = 40


def _short_desc(title: str) -> str:
    """Return *title* truncated to the canonical item-shape width.

    Empty input → empty output (caller suppresses the ``(desc)`` chunk).
    Titles within budget pass through verbatim; longer ones are tail-elided
    with a Unicode ellipsis so the rendered width never exceeds
    ``_ITEM_DESC_LEN``.
    """
    if not title:
        return ""
    if len(title) <= _ITEM_DESC_LEN:
        return title
    return title[: _ITEM_DESC_LEN - 1] + "…"


@dataclass(frozen=True, slots=True)
class _PRRef:
    iid: int
    url: str
    annotation: str
    # #1113 enhancement: when a ``ReviewRequestPost`` row exists for this
    # MR's URL, the renderer surfaces a clickable Slack permalink chunk so
    # the operator can jump from the statusline straight to the review
    # thread. Empty string when no post recorded — the chunk is omitted.
    review_permalink: str = ""
    # #1156: the MR's tracker title (``my_prs`` scanner payload). The
    # renderer surfaces it as ``!N (title)`` so the operator can scan
    # which MR is which without hovering for the OSC-8 link target.
    title: str = ""


@dataclass(frozen=True, slots=True)
class _IssueRef:
    label: str
    url: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class _ReassignRef:
    """An ``unassigned`` disposition that carries who it moved from/to.

    Lets the statusline render ``reassigned (from <old> → to <new>): #N``
    instead of a bare ``reassigned`` the user can't interpret.
    """

    ref: _IssueRef
    old_owner: str
    new_owners: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LinkCtx:
    """Renderer-side link deps bundled so item-shape helpers take 5 args.

    The two travel together (the link formatter respects ``colorize`` to
    choose OSC-8 vs. ``text <url>`` fallback), so passing them as a single
    struct keeps ``_render_canonical_item``'s signature small and lets the
    caller build the context once per render pass.
    """

    colorize: bool
    link: _LinkFn


@dataclass(frozen=True, slots=True)
class _OverlayActionRefs:
    """One overlay's slice of classified refs for the action-needed row.

    Bundling the three ref collections keeps ``_render_action_line``'s
    signature small (composition over a long positional list).
    """

    pr_refs: list[_PRRef]
    disposition_refs: dict[str, list[_IssueRef]]
    ready_refs: list[_IssueRef]
    reassign_refs: list[_ReassignRef] = field(default_factory=list)
    stale_refs: list[_IssueRef] = field(default_factory=list)


def _render_canonical_item(
    *,
    label: str,
    url: str,
    title: str,
    child_refs: list[_PRRef],
    ctx: _LinkCtx,
) -> str:
    """Render one item in the canonical statusline shape (#1015, #1156).

    ``#N (short desc) !M1 (MR1 title) !M2 (MR2 title)`` — every number
    is a hyperlink, the description is omitted when empty, each MR
    carries its title (#1156), and MRs are space-separated. The MRs are
    no longer wrapped in an outer ``(…)`` group — the per-MR title chunk
    is already parenthesised, so an outer group would double-bracket.

    *ctx* bundles the renderer-side link formatter (OSC-8 vs.
    ``text <url>`` fallback) with the ``colorize`` flag, so this module
    stays free of the rendering module's colorize toggling.
    """
    text = ctx.link(label, url, colorize=ctx.colorize)
    desc = _short_desc(title)
    if desc:
        text += f" ({desc})"
    if child_refs:
        text += " " + " ".join(_format_mr_ref(r, ctx) for r in child_refs)
    # #1113 enhancement: append a clickable Slack permalink chunk per child
    # MR whose ``ReviewRequestPost`` row recorded a thread post, so the
    # operator can jump from the statusline straight to the review thread.
    review_links = [
        ctx.link(f"review !{r.iid}", r.review_permalink, colorize=ctx.colorize)
        for r in child_refs
        if r.review_permalink
    ]
    if review_links:
        text += f" ({', '.join(review_links)})"
    return text


def _format_mr_ref(ref: _PRRef, ctx: _LinkCtx) -> str:
    """Render one MR ref in the ``!N (title)`` shape (#1156).

    The ``(title)`` chunk is appended *outside* the clickable ``!N`` so the
    hyperlink target stays small and the title is readable when ANSI
    sequences are stripped. ``title`` is truncated to the canonical 40-char
    budget via :func:`_short_desc`. The legacy annotation (``draft_count``,
    ``pipeline status``) survives as a separate ``(annotation)`` chunk so a
    titled draft renders as ``!N (title) (1 notes)``.
    """
    rendered = ctx.link(f"!{ref.iid}", ref.url, colorize=ctx.colorize)
    title = _short_desc(ref.title)
    if title:
        rendered += f" ({title})"
    if ref.annotation:
        rendered += f" ({ref.annotation})"
    return rendered
