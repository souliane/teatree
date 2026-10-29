"""The Notion failure taxonomy a headless run can act on.

A headless factory that gets one generic "Notion error" produces nothing and
says nothing useful about why. Each condition below needs a DIFFERENT human
action, so each is its own class carrying its own exit code:

===========================  ====  =============================================
Class                        Exit  What the operator must do
===========================  ====  =============================================
:class:`NotionTokenMissingError`     3  create the integration, store its token
:class:`NotionBadTokenError`         4  the token is rejected — rotate/replace it
:class:`NotionCapabilityDeniedError` 5  grant the integration the capability
:class:`NotionNotSharedError`        6  the bot cannot see the id — check the share, the id, the workspace
:class:`NotionObjectNotFoundError`   7  the reference is not a Notion object at all
:class:`NotionRateLimitedError`      8  back off — Notion is throttling
:class:`NotionWriteNotLandedError`   9  the write reported success but did not land
:class:`NotionAmbiguousSectionError` 10  two headings match — a human picks one
:class:`NotionUnsupportedMarkdownError` 11  the body uses a construct we won't guess at
:class:`NotionPropertyNotFoundError` 12  the page carries no property by that name
:class:`NotionUnwritablePropertyError` 13  that property cannot take that plain text
:class:`NotionPageNotLiveError`      14  the page is dead (or unprovable) — read the current one
:class:`NotionSectionNotFoundError`  15  --after-heading named no heading the page carries — supply an existing one
:class:`NotionUncopyableBlockError`  16  the block cannot be re-posted — copy without it
:class:`NotionWriteRefusedError`     17  the write guard refused the target — write only under an allowed root
:class:`NotionIncompleteEnumerationError` 18  part of the page was unreadable — the set is not the whole set
:class:`NotionAnchorError`           19  the replace anchor is not one editable span — narrow or widen the old text
:class:`NotionBlockChangedError`     20  the block changed after it was read — re-plan against its current text
:class:`NotionDiscussionNotFoundError` 21  the reply's discussion is not under the page — list them with `comments`
:class:`NotionWriteUnverifiedError`  22  a write was sent but its outcome is unknown — re-read before retrying
:class:`NotionWriteNotApprovedError` 23  no recorded approval covers this write — record one, then re-run
===========================  ====  =============================================

The distinction that costs the most to get wrong is **cannot-see vs bad-token**.
Notion answers an unseen page with ``404 object_not_found`` — the same shape a
bad token would eventually produce downstream — so :class:`NotionErrorClassifier`
resolves it by probing the token's own identity (``GET /v1/users/me``) before
deciding: a token that authenticates proves the bot cannot see this id, and the
message names that bot and the checks that separate the three causes the API
cannot: not shared with the integration, the id no longer exists, another workspace.

That 404 and :class:`NotionObjectNotFoundError` stay separate on purpose:
``NotionObjectNotFoundError`` is raised only where non-existence is PROVEN locally
(the reference is not a Notion id) or by Notion's own ``validation_error``, never
inferred from a 404 whose real cause is unknowable.
"""

import re
from collections.abc import Callable

import httpx

# Anchored on non-hex boundaries so a slug whose tail happens to be hex
# ("…-Deface-<id>") cannot shift the window and yield a plausible wrong id.
_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_DASHED_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_TOO_MANY_REQUESTS = 429
_BAD_REQUEST = 400


class NotionError(RuntimeError):
    """Base for every condition the Notion surface fails loudly on.

    ``exit_code`` is the process status the CLI exits with, so an unattended
    caller can branch on the condition without parsing the message.
    """

    exit_code = 2


class NotionTokenMissingError(NotionError):
    """No integration token resolved from the environment or the ``pass`` store."""

    exit_code = 3


class NotionBadTokenError(NotionError):
    """Notion rejected the token itself (HTTP 401) — it is invalid or revoked."""

    exit_code = 4


class NotionCapabilityDeniedError(NotionError):
    """The integration is shared onto the object but lacks the capability used.

    Notion's ``restricted_resource`` (HTTP 403): the token authenticates and the
    object is reachable, but the integration's capability set does not include
    what the request needs (read content, update content, insert content, or
    read comments). Fixed in the integration's own settings, not by sharing.
    """

    exit_code = 5


class NotionNotSharedError(NotionError):
    """The bot cannot see this id (HTTP 404, token verified).

    Notion answers 404 alike for an object not shared with the integration, an id
    that no longer exists, and an object in another workspace, so the cause is
    never asserted: the message lists all three. Raised only after the token has
    been proven to authenticate, so this is never a disguised bad-token failure.
    """

    exit_code = 6


class NotionObjectNotFoundError(NotionError):
    """The reference is not a Notion object id (proven, not inferred).

    Either the string carries no 32-hex id at all, or Notion answered the id with
    ``validation_error``. Distinct from :class:`NotionNotSharedError`, which is an
    id Notion answered 404 and the bot therefore cannot see.
    """

    exit_code = 7


class NotionRateLimitedError(NotionError):
    """Notion is throttling (HTTP 429) after the bounded retries were exhausted."""

    exit_code = 8

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotionWriteNotLandedError(NotionError):
    """A write reported success but the re-fetch did not confirm it.

    The whole reason the write path verifies: Notion can answer ``200`` on a
    block mutation that leaves the page unchanged, and a caller trusting the
    status code then reports a delivery that never happened.
    """

    exit_code = 9


class NotionAmbiguousSectionError(NotionError):
    """More than one heading on the page matches the owned section.

    A duplicate means an earlier run already went wrong; guessing which to keep
    compounds it, so the write stops and a human decides.
    """

    exit_code = 10


class NotionUnsupportedMarkdownError(NotionError):
    """The body uses a construct the block builder will not guess a mapping for.

    Refusing beats silently dropping the line: a section that lost its table is
    worse than a run that failed naming the line it could not represent.
    """

    exit_code = 11


class NotionPropertyNotFoundError(NotionError):
    """The page carries no property with the requested name.

    Proven by reading the page, not inferred: a property lives on the page
    object rather than in its blocks, and a merely misspelled name reads back
    exactly like an absent one, so the message lists the names the page does
    carry instead of leaving the caller to guess which it meant.
    """

    exit_code = 12


class NotionUnwritablePropertyError(NotionError):
    """The property exists but cannot be set from the given plain string.

    Either its type has no faithful plain-text form (a formula, a rollup, a
    relation, a people or files field — several of which Notion refuses to
    write at all), or the text is not a value that type can hold (``maybe`` for
    a checkbox, ``soon`` for a date). Refusing beats writing something else
    under a name the caller believes it just set.
    """

    exit_code = 13


class NotionPageNotLiveError(NotionError):
    """The page is archived, in the trash, or cannot be proven to be the current version.

    The one condition on this surface that a caller cannot see for itself: an
    archived page renders as COMPLETELY current — full body, acceptance criteria,
    a ``Status`` still reading "In Progress", live comment threads — so a
    superseded spec is read as the requirement and the work built against it is
    the WRONG work, not merely wasted work. A warning appended to the body is no
    control at all, because the body is what gets read; the read fails instead.

    Deliberately raised for the UNKNOWN verdict too (see
    :mod:`teatree.backends.notion.liveness`): a liveness this surface could not
    establish is not one it may act on, and "I could not check" is exactly what
    the failure looks like from the inside.
    """

    exit_code = 14


class NotionSectionNotFoundError(NotionError):
    """``--after-heading`` named a heading the page does not carry.

    Its own condition rather than a fallback to appending at the end: a
    positioned write whose position is missing has no correct place to land, and
    the end of the page is the one place the caller explicitly did not ask for.
    """

    exit_code = 15


class NotionUncopyableBlockError(NotionError):
    """A fetched block has no faithful POST-able form, so the copy stops.

    Either the block is a pointer to an object a copy cannot bring with it (a
    child page or database, a synced block, one Notion itself calls
    ``unsupported``), or its asset is Notion-hosted behind a signed URL that
    expires — a copy of which renders today and 404s next week. Refusing names
    the block; silently dropping it hands back a section missing content the
    caller believes it copied.
    """

    exit_code = 16


class NotionWriteRefusedError(NotionError):
    """The write guard refused the target: a write-denied root is above it, or no write-allowed root is."""

    exit_code = 17


class NotionIncompleteEnumerationError(NotionError):
    """An enumeration could not read part of what it walked, so its result is a subset.

    Its own condition rather than a shorter list, because the two are otherwise
    indistinguishable: a caller asking "what discussions are on this page?" acts
    on whatever comes back, and a partial answer that reads as complete is how a
    requirement gets written from a comment nobody could see.
    """

    exit_code = 18


class NotionAnchorError(NotionError):
    """The text a replace anchors on does not resolve to exactly one span that can be edited safely.

    Zero or several occurrences, a span that crosses blocks, or one that crosses runs formatted
    differently — each would make the write guess where to land or silently flatten formatting, so
    the replace stops and names what it found.
    """

    exit_code = 19


class NotionBlockChangedError(NotionError):
    """The block a replace planned against changed before the write, so the write would overwrite that edit."""

    exit_code = 20


class NotionDiscussionNotFoundError(NotionError):
    """A reply named a discussion the enumeration did not find under the page."""

    exit_code = 21


class NotionWriteUnverifiedError(NotionError):
    """A write reached Notion, or may have, but its response or the read-back failed, so it may have landed."""

    exit_code = 22


class NotionWriteNotApprovedError(NotionError):
    """The MCP write path found no unconsumed recorded approval scoped to exactly this write."""

    exit_code = 23


def describe_failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"


def normalize_object_id(reference: str) -> str:
    """Return the dashed Notion id for a page/database *reference*.

    Accepts a bare id (dashed or not) and any Notion URL that carries one —
    ``https://www.notion.so/Some-Title-<32 hex>``,
    ``https://www.notion.so/<workspace>/<32 hex>?v=…``. Raises
    :class:`NotionObjectNotFoundError` when the string carries no id, which is the
    one case where non-existence is provable without asking Notion.
    """
    candidate = reference.strip().rstrip("/")
    dashed = _DASHED_UUID.search(candidate)
    if dashed is not None:
        return dashed.group(0).lower()
    matches = _HEX32.findall(candidate)
    if not matches:
        msg = (
            f"{reference!r} carries no Notion object id — expected a 32-character id, "
            "a dashed UUID, or a notion.so URL ending in one"
        )
        raise NotionObjectNotFoundError(msg)
    raw = matches[-1].lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


class NotionErrorClassifier:
    """Turn an ``httpx`` failure into the taxonomy above, or re-raise it.

    *identity* is a zero-arg probe of the token's own identity
    (``GET /v1/users/me``) used ONLY on the 404 branch, to separate "the bot cannot
    see this id" from "the token is bad", and to name the bot. It is called
    lazily so the happy path never pays for it, and a probe that itself fails
    with 401 turns the verdict into :class:`NotionBadTokenError`.

    A status the taxonomy has no verdict for (5xx, a transient blip) is
    re-raised untouched — the retry transport and the callers above already
    treat those as retryable, and inventing a verdict for them would hide it.
    """

    def __init__(self, identity: Callable[[], str]) -> None:
        self._identity = identity

    def raise_for(self, response: httpx.Response, *, target: str) -> None:
        """Raise the classified error for a failed *response*; return on success."""
        if not response.is_error:
            return
        code = self._notion_code(response)
        raise self._verdict(response, target=target, code=code)

    def _verdict(self, response: httpx.Response, *, target: str, code: str) -> BaseException:
        status = response.status_code
        if status == _UNAUTHORIZED:
            return NotionBadTokenError(
                f"Notion rejected the integration token (HTTP 401) while reading {target}. "
                "The token is invalid, revoked, or belongs to a deleted integration — "
                "issue a new internal integration secret and store it again."
            )
        if status == _FORBIDDEN:
            return NotionCapabilityDeniedError(
                f"the integration lacks the capability this request needs on {target} "
                f"(HTTP 403 {code}). Open the integration's settings in Notion and grant "
                "the read/update/insert-content and read-comment capabilities it is missing."
            )
        if status == _BAD_REQUEST and code == "validation_error":
            return NotionObjectNotFoundError(
                f"Notion does not recognise {target} as an object id (HTTP 400 validation_error): "
                f"{self._message(response)}"
            )
        if status == _NOT_FOUND:
            return self._not_found_verdict(target)
        if status == _TOO_MANY_REQUESTS:
            return NotionRateLimitedError(
                f"Notion is rate-limiting this integration (HTTP 429) on {target}; "
                "the bounded retries were exhausted. Retry after the cooldown below.",
                retry_after=self._retry_after(response),
            )
        return self._http_error(response)

    def _not_found_verdict(self, target: str) -> BaseException:
        try:
            who = self._identity()
        except NotionBadTokenError as exc:
            return exc
        return NotionNotSharedError(
            f"{who} cannot see {target} (Notion answers 404). "
            "Possible causes: it is not shared with this integration, its id no longer exists, "
            "or it is in another workspace. "
            "Check: ••• → Connections includes this integration (match name and workspace above; "
            "`t3 notion whoami` shows the bot id); "
            "the URL's id is current (duplicating or recreating a page changes it); "
            "its parent is visible to this bot."
        )

    @staticmethod
    def _http_error(response: httpx.Response) -> BaseException:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return exc
        return NotionError(f"unclassifiable Notion response: HTTP {response.status_code}")

    @staticmethod
    def _notion_code(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("code", "")) if isinstance(body, dict) else ""

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        return str(body.get("message", "")) if isinstance(body, dict) else ""

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After", "").strip()
        try:
            return float(raw)
        except ValueError:
            return None
