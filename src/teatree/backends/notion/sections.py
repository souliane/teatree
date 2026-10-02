"""Block-scoped replacement of ONE named section on a Notion page.

The idempotency primitive the ``/prd-agent`` and ``/bdd-test-creation`` skills
are built around: each owns exactly one H2 section identified by its heading
string, and rewrites that section — and nothing else — on every run.

**Why this is block-scoped and never a whole-page write.** Notion attaches
comments and discussions to individual BLOCKS. A whole-page rewrite
(``replace_content`` on the interactive connector) re-creates every block, so
every discussion on the page dies with the block it hung off — silently,
unrecoverably, whether or not the text changed. This module therefore never
touches a block outside the resolved section: it appends the new body directly
after the owned heading, archives only the old body blocks it enumerated, and
patches a legacy heading IN PLACE (preserving that block's id, and with it its
discussions) rather than deleting and re-creating it. There is no whole-page
operation here to reach for by mistake.

**Two section shapes, both bounded.** A toggle heading owns its body as CHILDREN
of the heading block, so the boundary is the block tree itself. A plain heading's
body is the following siblings up to the next heading of the same or higher
level. :class:`SectionLocator` resolves either into the same
:class:`ResolvedSection` shape, so the writer has one code path.

**Verification is not optional.** :meth:`SectionWriter.replace` re-fetches after
writing and refuses to report success unless the new text is present, the old
body is gone, and exactly one canonical heading remains — because Notion answers
``200`` on writes that do not land, and a caller trusting the status code
reports a delivery that never happened.
"""

import dataclasses
import re
import unicodedata
from typing import cast

from teatree.backends.notion.blocks import build_blocks, heading_block
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionAmbiguousSectionError, NotionWriteNotLandedError
from teatree.backends.notion.markdown import BlockMarkdownRenderer, rich_text_plain
from teatree.types import RawAPIDict

_HEADING_TYPES = ("heading_1", "heading_2", "heading_3")
# Escaped codepoints, not the literal glyphs: hyphen, the U+2010..U+2015 dash
# family (the em dash the canonical heading uses lives here), and the minus sign.
_DASHES = re.compile(r"[-\u2010-\u2015\u2212]")
_ATTRIBUTE_BLOCK = re.compile(r"\s*\{[^}]*\}\s*$")
_WHITESPACE = re.compile(r"\s+")

#: A line shorter than this is punctuation or a list marker, not identifying text.
_MIN_PROBE_LENGTH = 3


def normalize_heading(heading: str) -> str:
    """Reduce a heading to the key the owned-section match is made on.

    Drops a leading ``##`` marker, a trailing ``{...}`` attribute block and a
    leading emoji; unifies every dash character; collapses whitespace; lower-cases.
    So ``## 🔧 /prd-agent — engineering delivery notes {toggle="true"}`` and the
    plain text Notion returns for that same heading block both reduce to
    ``/prd-agent - engineering delivery notes`` — which is what makes the heading
    an idempotency key rather than a label.
    """
    text = heading.strip()
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = _ATTRIBUTE_BLOCK.sub("", text)
    text = _DASHES.sub("-", text)
    text = "".join(char for char in text if not _is_pictographic(char))
    return _WHITESPACE.sub(" ", text).strip().lower()


def _is_pictographic(char: str) -> bool:
    # Emoji live in the "So" (symbol, other) category; letters, digits,
    # punctuation and the dashes normalized above all fall outside it.
    return unicodedata.category(char) == "So"


@dataclasses.dataclass(frozen=True)
class ResolvedSection:
    """One located section: its heading block and the exact blocks that are its body.

    *toggle* records which shape it is — a toggle heading owning its body as
    children, or a plain heading followed by sibling blocks — because the append
    target differs (the heading block itself vs. the page) while everything else
    about the write does not.
    """

    heading_id: str
    heading_text: str
    body_block_ids: tuple[str, ...]
    toggle: bool
    matched_legacy: bool

    @property
    def container_id(self) -> str:
        return self.heading_id if self.toggle else ""


class SectionLocator:
    """Find the single block-range on a page that a set of headings owns.

    *canonical* is the heading a new section is created with; *legacy* are older
    strings the same skill wrote before, adopted (renamed in place) rather than
    duplicated beside. Zero matches means "create"; more than one raises
    :class:`~teatree.backends.notion.errors.NotionAmbiguousSectionError` — a duplicate
    means an earlier run already went wrong, and guessing which to keep compounds
    it.
    """

    def __init__(self, client: NotionClient, *, canonical: str, legacy: tuple[str, ...] = ()) -> None:
        self._client = client
        self.canonical = canonical
        self._canonical_key = normalize_heading(canonical)
        self._legacy_keys = {normalize_heading(item) for item in legacy} - {self._canonical_key}

    def owns(self, heading_text: str) -> bool:
        key = normalize_heading(heading_text)
        return key == self._canonical_key or key in self._legacy_keys

    def resolve(self, page_id: str) -> ResolvedSection | None:
        blocks = self._client.list_block_children(page_id)
        matches = [(index, block) for index, block in enumerate(blocks) if self._is_owned_heading(block)]
        if not matches:
            return None
        if len(matches) > 1:
            texts = [heading_plain_text(block) for _, block in matches]
            msg = (
                f"{len(matches)} headings on page {page_id} match the owned section {self.canonical!r}: "
                f"{texts}. A duplicate means an earlier run went wrong — a human decides which to keep. "
                "Nothing was written."
            )
            raise NotionAmbiguousSectionError(msg)
        index, block = matches[0]
        return self._build(block, blocks, index)

    def _build(self, block: RawAPIDict, blocks: list[RawAPIDict], index: int) -> ResolvedSection:
        heading_id = str(block.get("id", ""))
        text = heading_plain_text(block)
        toggle = is_toggle_heading(block)
        if toggle:
            body_ids = tuple(str(child.get("id", "")) for child in self._client.list_block_children(heading_id))
        else:
            body_ids = tuple(str(sibling.get("id", "")) for sibling in _following_body(blocks, index))
        return ResolvedSection(
            heading_id=heading_id,
            heading_text=text,
            body_block_ids=body_ids,
            toggle=toggle,
            matched_legacy=normalize_heading(text) != self._canonical_key,
        )

    def _is_owned_heading(self, block: RawAPIDict) -> bool:
        return heading_level(block) > 0 and self.owns(heading_plain_text(block))


def heading_level(block: RawAPIDict) -> int:
    """Return 1/2/3 for a heading block, 0 for anything else."""
    block_type = str(block.get("type", ""))
    return _HEADING_TYPES.index(block_type) + 1 if block_type in _HEADING_TYPES else 0


def heading_plain_text(block: RawAPIDict) -> str:
    rich_text = _heading_payload(block).get("rich_text")
    return rich_text_plain(cast("list[RawAPIDict]", rich_text)) if isinstance(rich_text, list) else ""


def is_toggle_heading(block: RawAPIDict) -> bool:
    return bool(_heading_payload(block).get("is_toggleable"))


def _heading_payload(block: RawAPIDict) -> RawAPIDict:
    payload = block.get(str(block.get("type", "")))
    return cast("RawAPIDict", payload) if isinstance(payload, dict) else {}


def _following_body(blocks: list[RawAPIDict], index: int) -> list[RawAPIDict]:
    """The sibling blocks a plain heading owns: up to the next same-or-higher heading."""
    level = heading_level(blocks[index])
    body: list[RawAPIDict] = []
    for block in blocks[index + 1 :]:
        next_level = heading_level(block)
        if 0 < next_level <= level:
            break
        body.append(block)
    return body


@dataclasses.dataclass(frozen=True)
class SectionWriteResult:
    """What a write did, in the vocabulary the skills report per section."""

    outcome: str
    heading: str
    blocks_written: int
    blocks_archived: int


class SectionWriter:
    """Create or replace one owned section, verifying the result before reporting it.

    Every write goes append-first, archive-second. A failure between the two
    leaves the page carrying both the new and the old body — visible, recoverable,
    and caught by the verification — whereas archiving first would leave an empty
    section if the append then failed.
    """

    def __init__(self, client: NotionClient, locator: SectionLocator) -> None:
        self._client = client
        self._locator = locator

    def create(self, page_id: str, body_markdown: str, *, toggle: bool = True) -> SectionWriteResult:
        """Append the section at the end of the page, with the canonical heading."""
        heading = heading_block(self._locator.canonical, level=2, toggle=toggle)
        body = build_blocks(body_markdown)
        if toggle:
            heading["heading_2"] = {**_as_dict(heading["heading_2"]), "children": body}
            self._client.append_block_children(page_id, [heading])
        else:
            self._client.append_block_children(page_id, [heading, *body])
        self._verify(page_id, body_markdown, archived=())
        return SectionWriteResult(
            outcome="created",
            heading=self._locator.canonical,
            blocks_written=len(body) + 1,
            blocks_archived=0,
        )

    def replace(self, page_id: str, section: ResolvedSection, body_markdown: str) -> SectionWriteResult:
        """Rewrite *section*'s body in place, leaving every other block untouched."""
        body = build_blocks(body_markdown)
        container = section.container_id or page_id
        after = "" if section.toggle else section.heading_id
        self._client.append_block_children(container, body, after=after)
        if section.matched_legacy:
            self._adopt_heading(section)
        for block_id in section.body_block_ids:
            self._client.delete_block(block_id)
        self._verify(page_id, body_markdown, archived=section.body_block_ids)
        return SectionWriteResult(
            outcome="replaced",
            heading=self._locator.canonical,
            blocks_written=len(body),
            blocks_archived=len(section.body_block_ids),
        )

    def _adopt_heading(self, section: ResolvedSection) -> None:
        """Rename a legacy heading in place, keeping the block id and its discussions.

        Only ``rich_text`` is sent. A payload that also restated ``is_toggleable``
        would silently flip a toggle heading to a plain one — which relocates the
        whole section body from the heading's children to its siblings.
        """
        payload = _as_dict(heading_block(self._locator.canonical, level=2)["heading_2"])
        self._client.update_block(section.heading_id, {"heading_2": {"rich_text": payload["rich_text"]}})

    def _verify(self, page_id: str, body_markdown: str, *, archived: tuple[str, ...]) -> None:
        """Re-fetch and refuse to report success unless the write actually landed.

        Three independent checks, because Notion can answer ``200`` on a write
        that changes nothing: exactly one heading still matches the owned set (no
        duplicate created, no orphaned legacy left), the new body's text is
        present under it, and every block that was supposed to be archived is
        gone.
        """
        section = self._locator.resolve(page_id)
        if section is None:
            msg = (
                f"the write reported success but page {page_id} carries no section matching "
                f"{self._locator.canonical!r} on re-fetch — nothing landed."
            )
            raise NotionWriteNotLandedError(msg)
        landed_ids = set(section.body_block_ids)
        surviving = [block_id for block_id in archived if block_id in landed_ids]
        if surviving:
            msg = (
                f"the write reported success but {len(surviving)} old block(s) of section "
                f"{self._locator.canonical!r} survived on re-fetch ({surviving[:3]}) — the page now "
                "carries both the old and the new body. Nothing was archived twice; a human must "
                "remove the stale blocks."
            )
            raise NotionWriteNotLandedError(msg)
        self._verify_text_present(page_id, section, body_markdown)

    def _verify_text_present(self, page_id: str, section: ResolvedSection, body_markdown: str) -> None:
        rendered = BlockMarkdownRenderer(self._client.list_block_children).render(
            self._section_body_blocks(page_id, section)
        )
        missing = [probe for probe in _verification_probes(body_markdown) if probe not in rendered]
        if missing:
            msg = (
                f"the write reported success but the re-fetched section {self._locator.canonical!r} "
                f"on page {page_id} does not contain {missing[0]!r}. Treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)

    def _section_body_blocks(self, page_id: str, section: ResolvedSection) -> list[RawAPIDict]:
        """The live block dicts of *section*'s body, whichever shape it has."""
        if section.toggle:
            return self._client.list_block_children(section.heading_id)
        wanted = set(section.body_block_ids)
        return [block for block in self._client.list_block_children(page_id) if str(block.get("id", "")) in wanted]


def _verification_probes(body_markdown: str) -> list[str]:
    """The text fragments the re-fetch must contain for the write to count as landed.

    The first and last non-trivial lines, stripped of Markdown syntax: a section
    whose opening and closing survived a round trip did land, and keying on two
    ends rather than the whole body keeps the check robust to the renderer's
    lossy block types without weakening it to a length comparison.
    """
    lines = [_plain_probe(line) for line in body_markdown.splitlines()]
    candidates = [line for line in lines if len(line) > _MIN_PROBE_LENGTH]
    if not candidates:
        return []
    return [candidates[0]] if len(candidates) == 1 else [candidates[0], candidates[-1]]


def _plain_probe(line: str) -> str:
    stripped = re.sub(r"^[\s>#*\-\d.)\[\]x ]+", "", line.strip())
    return re.sub(r"[*`~_|]", "", stripped).strip()


def _as_dict(value: object) -> RawAPIDict:
    return cast("RawAPIDict", value) if isinstance(value, dict) else {}
