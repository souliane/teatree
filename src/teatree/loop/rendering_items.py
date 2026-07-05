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

import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

from teatree.url_classify import is_github_pr_url


class _LinkFn(Protocol):
    def __call__(self, text: str, url: object, *, colorize: bool) -> str: ...


# Canonical statusline chip shape: ``#N (terse topic) !M1 !M2``. The
# ``(topic)`` chunk is a 2-3 word gist, not the full commit subject —
# a chip is a glance-target, not a changelog. The full title still lives
# on the OSC-8 hyperlink target, so nothing is lost.
_TOPIC_WORDS = 3
_TOPIC_MAX_LEN = 24

# Conventional-commit / scoped prefix (``fix:``, ``feat(loop):``,
# ``techdebt:``) carries no topic signal on a chip — every chip is already
# work-in-flight — so it is stripped before the word budget is applied.
_CC_PREFIX_RE = re.compile(r"^[a-z][\w-]*(?:\([^)]*\))?!?:\s*", re.IGNORECASE)


def _short_desc(title: str) -> str:
    """Return a terse 2-3 word topic for *title* (the chip ``(topic)`` chunk).

    Empty input → empty output (caller suppresses the ``(desc)`` chunk).
    The leading conventional-commit prefix (``fix:``, ``feat(loop):``,
    ``techdebt:``) is dropped, then the first :data:`_TOPIC_WORDS` words are
    kept and capped at :data:`_TOPIC_MAX_LEN` chars with a single-codepoint
    Unicode ellipsis — so a long commit subject like
    ``techdebt: refactor PLW0717 try-clause-too-long across modules``
    collapses to ``refactor PLW0717 try-clause-…`` rather than a 40-char
    slice of the whole subject.
    """
    if not title:
        return ""
    stripped = _CC_PREFIX_RE.sub("", title, count=1).strip()
    if not stripped:
        stripped = title.strip()
    words = stripped.split()
    topic = " ".join(words[:_TOPIC_WORDS])
    if len(topic) > _TOPIC_MAX_LEN:
        return topic[: _TOPIC_MAX_LEN - 1].rstrip() + "…"
    return topic


@dataclass(frozen=True, slots=True)
class _PRRef:
    iid: int
    url: str
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

    ``colorize`` and ``link`` travel together (the link formatter respects
    ``colorize`` to choose OSC-8 vs. ``text <url>`` fallback), so passing them
    as a single struct keeps ``_render_canonical_item``'s signature small and
    lets the caller build the context once per render pass. ``search_base`` is
    the overlay's tracker-search URL prefix (PR-17): when a ref has no canonical
    URL, the chip links to ``search_base + <term>`` instead of rendering as bare
    text — empty when the overlay's tracker cannot be resolved (last-resort bare).
    """

    colorize: bool
    link: _LinkFn
    search_base: str = ""


_DIGITS_RE = re.compile(r"\d+")


def _search_term(label: str) -> str:
    """Extract the searchable term from a chip label (``#214`` → ``214``).

    Prefers the first run of digits (the ticket/MR number); when the label
    carries no number (a title-slug fallback ref) the whole label stripped of
    its ``⚡#!`` chip decoration is used.
    """
    match = _DIGITS_RE.search(label)
    if match:
        return match.group(0)
    return label.lstrip("⚡#!").strip()


def _effective_url(url: str, label: str, search_base: str) -> str:
    """Resolve the URL a chip links to: the canonical one, else a tracker search (PR-17).

    A real ``http(s)`` *url* is returned as-is. Otherwise — an unknown or
    404'd canonical URL — the chip falls back to the overlay's tracker search
    (``search_base`` + the label's search term) so it stays clickable rather
    than rendering as bare text. Returns ``""`` only when even the search base
    is unresolvable (the last-resort bare case).
    """
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    if not search_base:
        return ""
    return f"{search_base}{quote(_search_term(label))}"


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


def _chip_prefix(url: str) -> str:
    """Return ``#`` for GitHub PR URLs, ``!`` otherwise (#1377).

    Everything that is not a GitHub PR URL (GitLab MRs, unknown / blank URLs)
    gets the ``!`` default so the pre-existing GitLab behaviour is preserved
    when the URL is missing.
    """
    return "#" if is_github_pr_url(url) else "!"


def _render_canonical_item(
    *,
    label: str,
    url: str,
    title: str,
    child_refs: list[_PRRef],
    ctx: _LinkCtx,
) -> str:
    """Render one item in the terse statusline shape (#1377, binding spec).

    ``#N (topic !M1 !M2 …)`` — every number is a hyperlink, topic and
    chips share one pair of parens, and the parens are suppressed when
    both topic and chips are absent so a bare ticket reads ``#N`` with no
    trailing decoration. GitHub PRs render with the ``#`` chip prefix;
    GitLab MRs keep ``!``. Per the binding spec the renderer adds no
    per-MR title, no annotation chunk, and no review-permalink suffix —
    richer per-MR signal belongs in dedicated zones. A ref with no canonical
    URL links to the overlay's tracker search rather than rendering bare (PR-17).
    """
    text = ctx.link(label, _effective_url(url, label, ctx.search_base), colorize=ctx.colorize)
    topic = _short_desc(title)
    chips = " ".join(_format_mr_ref(r, ctx) for r in child_refs)
    inner = " ".join(part for part in (topic, chips) if part)
    if inner:
        text += f" ({inner})"
    return text


def _format_mr_ref(ref: _PRRef, ctx: _LinkCtx) -> str:
    """Render one MR/PR chip as a bare clickable ``!<iid>`` or ``#<iid>`` (#1377).

    Per the binding spec the chip is just the number — no title, no
    annotation, no Slack permalink suffix. GitHub PR URLs render with
    the ``#`` prefix; GitLab MR URLs render with ``!``. A ref with no URL
    links to the overlay's tracker search rather than rendering bare (PR-17).
    """
    chip = f"{_chip_prefix(ref.url)}{ref.iid}"
    return ctx.link(chip, _effective_url(ref.url, chip, ctx.search_base), colorize=ctx.colorize)
