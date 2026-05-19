"""Colleague-MR review-shape gate (souliane/teatree#1114).

The binding rule from the review skill is **single terse INLINE
``Nit:``-prefixed comment** on a colleague's MR. The previous safety-net
was a memory entry (a guideline an agent could forget). This module is
the structural enforcement: every ``ReviewService`` publishing method
that takes a body routes through :func:`check_review_shape` before the
GitLab API call. When the gate refuses, the API call is never attempted
and no receipt DM (``notify_user_on_behalf_post``) fires for a blocked
post — short-circuited by the same ``(message, 1)`` return shape the
on-behalf gate uses.

Shape rules:

* **Own MR** (``mr.author == current_username``) — exempt. Own-MR
    reviews can be long-form (self-review summary, evidence block).
* **Colleague MR**, inline (``file`` and ``line`` both set) — generous
    cap (:data:`INLINE_NIT_CAP_SENTENCES`). Real findings tied to a
    specific diff line can legitimately span a few sentences.
* **Colleague MR**, MR-level prose (``file == ""`` and ``line == 0``)
    — tight cap (:data:`COLLEAGUE_MR_PROSE_CAP_SENTENCES`,
    :data:`COLLEAGUE_MR_PROSE_CAP_CHARS`). A 4-sentence MR-level note on a
    colleague MR is the exact shape the !6201 RED CARD violated.

The signature ``(api, encoded_repo, mr, body, inline)`` is
forge-neutral — future GitHub PR support is a single-method extension
(swap the GitLab GET for a GitHub one).
"""

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from teatree.backends.gitlab_api import GitLabHTTPClient

COLLEAGUE_MR_PROSE_CAP_SENTENCES = 2
COLLEAGUE_MR_PROSE_CAP_CHARS = 280
INLINE_NIT_CAP_SENTENCES = 4

_TTL_MR_AUTHOR = 300  # 5 minutes — mirrors `_TTL_USERNAME` shape

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")


def fetch_mr_author(api: "GitLabHTTPClient", encoded_repo: str, mr: int) -> str:
    """Return the MR author's username, cached for 5 minutes per ``(repo, mr)``.

    Reuses the :class:`~teatree.backends.gitlab_api.GitLabHTTPClient`
    response-cache machinery (``_set_cached`` / ``_get_cached``) so a
    second post on the same MR within the TTL skips the GitLab GET.
    The cache lookup is best-effort: a non-canonical API stub without
    the cache helpers degrades gracefully to an un-cached GET (the gate
    is still correct, just one extra GET per call).
    """
    cache_key = f"mr_author:{encoded_repo}:{mr}"
    get_cached = getattr(api, "_get_cached", None)
    if callable(get_cached):
        cached = get_cached(cache_key, _TTL_MR_AUTHOR)
        if cached is not None:
            return str(cached)
    try:
        data = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}")
    except Exception:  # noqa: BLE001 — network/auth failure → fail-open
        return ""
    author = ""
    if isinstance(data, dict):
        raw_author: object = data.get("author")
        if isinstance(raw_author, Mapping):
            raw_username = cast("Mapping[str, object]", raw_author).get("username", "")
            author = str(raw_username) if raw_username is not None else ""
    set_cached = getattr(api, "_set_cached", None)
    if callable(set_cached):
        set_cached(cache_key, author)
    return author


def is_colleague_mr(api: "GitLabHTTPClient", encoded_repo: str, mr: int) -> bool:
    """Whether the MR was authored by someone other than the current identity.

    Empty MR author or missing/empty current_username (both indicate a
    fetch failure, e.g. missing token, or a test stub) returns
    ``False`` — fail-open on the shape gate, because failing closed on
    an inability to read identity would silently break every on-behalf
    review post and break every existing test stub.
    """
    author = fetch_mr_author(api, encoded_repo, mr)
    if not author:
        return False
    current_username = getattr(api, "current_username", None)
    if not callable(current_username):
        return False
    me = current_username()
    if not me:
        return False
    return author != me


def _count_sentences(body: str) -> int:
    """Count sentence-terminating runs in ``body``.

    A "sentence" is anything ending in one or more ``.``/``!``/``?``
    followed by whitespace or end-of-string. Trailing prose without a
    terminator still counts (e.g. ``"S1. S2"`` is 2 sentences).
    """
    text = body.strip()
    if not text:
        return 0
    terminated = len(_SENTENCE_SPLIT_RE.findall(text))
    trailing = _SENTENCE_SPLIT_RE.split(text)[-1].strip()
    if trailing:
        return terminated + 1
    return terminated


def check_review_shape(
    *,
    api: "GitLabHTTPClient",
    encoded_repo: str,
    mr: int,
    body: str,
    inline: bool,
) -> str:
    """Return a non-empty steering error when the colleague-MR shape rule is breached.

    Returns ``""`` (proceed) when:

    * the MR is the current identity's own MR (carve-out), OR
    * the body fits the applicable cap (inline cap when ``inline`` is
        true, MR-level prose cap otherwise).

    The steering error names the concrete sentence count so the agent
    knows exactly what breached, and points at the inline ``Nit:`` form
    that satisfies the rule.
    """
    if not body:
        return ""
    if not is_colleague_mr(api, encoded_repo, mr):
        return ""

    sentence_count = _count_sentences(body)
    char_count = len(body)

    if inline:
        if sentence_count <= INLINE_NIT_CAP_SENTENCES:
            return ""
        return _steering_error(sentence_count, inline=True)

    if sentence_count <= COLLEAGUE_MR_PROSE_CAP_SENTENCES and char_count <= COLLEAGUE_MR_PROSE_CAP_CHARS:
        return ""
    return _steering_error(sentence_count, inline=False)


def _steering_error(sentence_count: int, *, inline: bool) -> str:
    """Build the actionable refusal message.

    The ``inline`` flag distinguishes "you posted MR-level prose, switch
    to inline" from "your inline note is too long, tighten it". Both
    point at the canonical ``t3 review post-comment ... --file ... --line ...``
    invocation, since that is the satisfying shape in either case.
    """
    cap = INLINE_NIT_CAP_SENTENCES if inline else COLLEAGUE_MR_PROSE_CAP_SENTENCES
    surface = "inline note" if inline else "MR-level prose"
    return (
        f"Refusing colleague-MR on-behalf post: {sentence_count}-sentence {surface} exceeds the "
        f"{cap}-sentence cap. Re-post as an inline Nit:-prefixed comment on the exact file:line "
        '(see skills/review § "Single terse inline nit"):\n'
        '  t3 review post-comment <repo> <mr> "Nit: ..." --file <path> --line <N>\n'
        "Or, if approving, run `t3 review approve <repo> <mr>` (no body needed)."
    )
