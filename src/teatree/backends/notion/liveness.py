"""Whether a Notion page is still the LIVE version of itself.

An archived Notion page renders as completely current: full requirements,
acceptance criteria, a ``Status`` property still reading "In Progress", live
comment threads. Nothing a reader naturally looks at says the page is dead, so a
superseded spec is read as THE requirement — and an acceptance criterion that
only the dead page states ("when the flag is off, the tooltip is absent") sends
an agent to build the very thing the current spec declines to ask for. That is
wrong work, not merely wasted work, which is why the read surface refuses on
this predicate rather than warning on it.

TWO SIGNALS, AND ONLY ONE OF THEM PROVES DEATH
----------------------------------------------
* ``archived`` / ``in_trash`` on the page object is the only signal the API
    exposes that CONCLUDES a page is dead. It is checked first and it is decisive.
* Membership of the page's own parent database is the corroboration. Calling it
    an independent detector would be dishonest: the probe queries the parent the
    PAGE ITSELF declares, so a row moved out of a database no longer names that
    database and is never asked about it — and Notion excludes archived rows from
    an unfiltered query anyway, which makes absence largely a consequence of the
    flag rather than a second opinion on it. What membership genuinely buys is the
    two things the flag cannot give: it turns a read this surface cannot vouch for
    into an explicit :attr:`Liveness.UNKNOWN` instead of a silent pass, and its
    result set is where the SUCCESSOR — the current version the caller must go read
    instead — is found.

So the predicate is a **disjunction for death and a conjunction for life**: any
one signal saying dead is enough (a page needs only one way to die), while LIVE
must be earned — every applicable signal evaluated, and every one of them
agreeing.

UNKNOWN IS A REFUSAL, NOT A PASS
--------------------------------
A membership probe that cannot reach its subject — the parent database is not
shared with this integration, the query is throttled, the row carries no title
to look itself up by — reports :attr:`Liveness.UNKNOWN`, and the read surface
treats UNKNOWN exactly like DEAD. Clearing an UNKNOWN is the same one-time human
grant the rest of this surface already requires: share the parent database with
the integration, the way each page is already shared.

The successor lookup is best-effort in the other direction: a probe that fails
can NEVER soften a verdict, only leave the recovery hint empty, which the
refusal then states plainly rather than guessing at.
"""

import dataclasses
from enum import StrEnum
from typing import Protocol, cast

import httpx

from teatree.backends.notion.errors import (
    NotionCapabilityDeniedError,
    NotionError,
    NotionNotSharedError,
    NotionPageNotLiveError,
    describe_failure,
)
from teatree.backends.notion.markdown import rich_text_plain
from teatree.types import RawAPIDict

_RULE = (
    "An archived or superseded page is not a weaker source, it is not a source at all: "
    "do not read it, ignore it entirely, and go find the more recent version."
)

#: How many rows the by-id membership scan reads before it stops and says so.
_MEMBERSHIP_SCAN_ROWS = 1000

_AUDIT = (
    "To read it anyway for a genuine audit — and stamp the output as dead so it cannot be "
    "mistaken for current — use `t3 notion audit-fetch <page> --reason '<why>'`. That is a "
    "postmortem tool, not a way to recover requirements from a page that no longer states them."
)


class NotionPageReader(Protocol):
    """The three reads the probe needs.

    Declared structurally here so this module never imports the concrete client —
    the client imports the probe, and one direction is all the graph may have.
    """

    def get_page(self, page_id: str) -> RawAPIDict: ...  # pragma: no branch

    def query_database(
        self, database_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100, max_rows: int = 0
    ) -> list[RawAPIDict]: ...  # pragma: no branch

    def query_data_source(
        self, data_source_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100, max_rows: int = 0
    ) -> list[RawAPIDict]: ...  # pragma: no branch


class Liveness(StrEnum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True, slots=True)
class PageIdentity:
    """Where a page says it lives, and how its row is recognised in that database."""

    database_id: str = ""
    data_source: bool = False
    title_property: str = ""
    title: str = ""
    title_match: RawAPIDict = dataclasses.field(default_factory=dict)

    @property
    def is_database_row(self) -> bool:
        return bool(self.database_id)

    @property
    def is_filterable(self) -> bool:
        return bool(self.database_id and self.title_property and self.title)

    @classmethod
    def of(cls, page: RawAPIDict) -> "PageIdentity":
        parent = page.get("parent")
        typed = cast("RawAPIDict", parent) if isinstance(parent, dict) else {}
        database_id = str(typed.get("database_id") or "")
        data_source_id = str(typed.get("data_source_id") or "")
        title_property, title = title_of(page)
        return cls(
            database_id=database_id or data_source_id,
            data_source=not database_id and bool(data_source_id),
            title_property=title_property,
            title=title,
            title_match=_title_match(page, title_property),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class LivenessVerdict:
    """What the probe concluded, why, and where the current version is."""

    state: Liveness
    reason: str
    detail: str
    successors: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        return self.state is Liveness.LIVE

    def headline(self, page_id: str) -> str:
        return f"page {page_id} is {self.state.value} [{self.reason}] — {self.detail}"

    def recovery(self) -> str:
        if self.successors:
            return "The current version looks like: " + ", ".join(self.successors)
        return (
            "The current version could NOT be resolved from the live database — find it yourself "
            "before using anything from this page, rather than falling back to reading this one."
        )

    def as_error(self, page_id: str) -> NotionPageNotLiveError:
        return NotionPageNotLiveError(f"{self.headline(page_id)}\n{_RULE}\n{self.recovery()}\n{_AUDIT}")


class PageLivenessProbe:
    """Decide whether a page may be handed to a caller as the current version."""

    def __init__(self, client: NotionPageReader) -> None:
        self._client = client

    def verdict(self, page_id: str) -> LivenessVerdict:
        page = self._client.get_page(page_id)
        identity = PageIdentity.of(page)
        if _flagged(page):
            return LivenessVerdict(
                state=Liveness.DEAD,
                reason="archived_flag",
                detail="Notion reports it as archived or in the trash, and it renders exactly like a current page",
                successors=self._successors(page_id, identity),
            )
        if not identity.is_database_row:
            return LivenessVerdict(
                state=Liveness.LIVE,
                reason="not_a_database_row",
                detail="its own flags say live, and it is no database's row, so there is no membership to corroborate",
            )
        if not identity.is_filterable:
            return LivenessVerdict(
                state=Liveness.UNKNOWN,
                reason="identity_unresolvable",
                detail=(
                    f"it is a row of database {identity.database_id} but carries no title to look itself up by, "
                    "so nothing corroborates its own flags"
                ),
            )
        return self._membership_verdict(page_id, identity)

    def _membership_verdict(self, page_id: str, identity: PageIdentity) -> LivenessVerdict:
        try:
            titled = self._rows_titled_like(identity)
            found = any(_same_id(row.get("id"), page_id) for row in titled)
            scanned = [] if found else self._rows(identity, db_filter=None, max_rows=_MEMBERSHIP_SCAN_ROWS)
        except NotionNotSharedError as exc:
            return LivenessVerdict(
                state=Liveness.UNKNOWN,
                reason="parent_database_unreadable",
                detail=(
                    f"its parent database {identity.database_id} could not be queried, so nothing corroborates "
                    "its own flags — check that the integration can see that database (usually: share it) "
                    f"and re-run: {exc}"
                ),
            )
        except NotionCapabilityDeniedError as exc:
            return LivenessVerdict(
                state=Liveness.UNKNOWN,
                reason="parent_database_unverified",
                detail=(
                    f"the integration lacks access to its parent database {identity.database_id} (HTTP 403: {exc}), "
                    "so its membership could not be verified — grant the integration the capability Notion names"
                ),
            )
        except (NotionError, httpx.HTTPError) as exc:
            return LivenessVerdict(
                state=Liveness.UNKNOWN,
                reason="parent_database_unverified",
                detail=(
                    f"querying its parent database {identity.database_id} failed ({describe_failure(exc)}), so its "
                    "membership could not be verified — transient or unsupported; retry, and open the page in "
                    "Notion if it persists"
                ),
            )
        if found or any(_same_id(row.get("id"), page_id) for row in scanned):
            by_id = "" if found else ", found by page id after the title filter missed it"
            return LivenessVerdict(
                state=Liveness.LIVE,
                reason="present_in_parent_database",
                detail=f"its own flags say live and database {identity.database_id} still returns it{by_id}",
            )
        capped = " (the scan stopped at its cap)" if len(scanned) >= _MEMBERSHIP_SCAN_ROWS else ""
        return LivenessVerdict(
            state=Liveness.UNKNOWN,
            reason="absent_from_parent_database",
            detail=(
                f"its own flags say live; the database query worked but did not return this page among rows titled "
                f"{identity.title!r} ({len(titled)} row(s) returned), and scanning {len(scanned)} row(s) of database "
                f"{identity.database_id} by page id did not find it either{capped} — it may have been superseded "
                "or moved out of that database; open it in Notion to confirm which version is current"
            ),
            successors=_successor_refs(_same_titled(titled, identity), page_id),
        )

    def _successors(self, page_id: str, identity: PageIdentity) -> tuple[str, ...]:
        """The live rows sharing this page's title — best effort, never a reason to soften a verdict."""
        if not identity.is_filterable:
            return ()
        try:
            rows = self._rows_titled_like(identity)
        except (NotionError, httpx.HTTPError):
            return ()
        return _successor_refs(_same_titled(rows, identity), page_id)

    def _rows_titled_like(self, identity: PageIdentity) -> list[RawAPIDict]:
        if not identity.title_match:
            return []
        return self._rows(identity, db_filter={"property": identity.title_property, "title": identity.title_match})

    def _rows(self, identity: PageIdentity, *, db_filter: RawAPIDict | None, max_rows: int = 0) -> list[RawAPIDict]:
        if identity.data_source:
            return self._client.query_data_source(identity.database_id, db_filter=db_filter, max_rows=max_rows)
        return self._client.query_database(identity.database_id, db_filter=db_filter, max_rows=max_rows)


def _title_match(page: RawAPIDict, title_property: str) -> RawAPIDict:
    """The title condition Notion can match: its title filter sees text runs only, never a mention's text."""
    properties = page.get("properties")
    prop = cast("RawAPIDict", properties).get(title_property) if isinstance(properties, dict) else None
    spans = cast("RawAPIDict", prop).get("title") if isinstance(prop, dict) else None
    runs = cast("list[RawAPIDict]", spans) if isinstance(spans, list) else []
    texts = [str(run.get("plain_text", "")) for run in runs if run.get("type", "text") == "text"]
    if len(texts) == len(runs) and "".join(texts):
        return {"equals": "".join(texts)}
    longest = max((text.strip() for text in texts), key=len, default="")
    return {"contains": longest} if longest else {}


def _same_titled(rows: list[RawAPIDict], identity: PageIdentity) -> list[RawAPIDict]:
    if "equals" in identity.title_match:
        return rows
    wanted = " ".join(identity.title.split()).casefold()
    return [row for row in rows if " ".join(title_of(row)[1].split()).casefold() == wanted]


def title_of(page: RawAPIDict) -> tuple[str, str]:
    """The name and plain text of the page's ``title`` property, or two empty strings."""
    properties = page.get("properties")
    carried = cast("RawAPIDict", properties) if isinstance(properties, dict) else {}
    for name, prop in carried.items():
        typed = cast("RawAPIDict", prop) if isinstance(prop, dict) else {}
        if typed.get("type") != "title":
            continue
        spans = typed.get("title")
        return name, rich_text_plain(cast("list[RawAPIDict]", spans)) if isinstance(spans, list) else ""
    return "", ""


def _flagged(payload: RawAPIDict) -> bool:
    return bool(payload.get("archived")) or bool(payload.get("in_trash"))


def _successor_refs(rows: list[RawAPIDict], page_id: str) -> tuple[str, ...]:
    candidates = (
        str(row.get("url") or row.get("id") or "")
        for row in rows
        if not _same_id(row.get("id"), page_id) and not _flagged(row)
    )
    return tuple(reference for reference in candidates if reference)


def _same_id(left: object, right: object) -> bool:
    bare = _bare(left)
    return bool(bare) and bare == _bare(right)


def _bare(value: object) -> str:
    return str(value or "").replace("-", "").lower()
