"""Colleague-MR review-shape gate (souliane/teatree#1114, loosened in #1159).

The binding rule from the review skill is **single terse INLINE
``Nit:``-prefixed comment** on a colleague's MR. The previous safety-net
was a memory entry (a guideline an agent could forget). This module is
the structural enforcement: every ``ReviewService`` publishing method
that takes a body routes through :func:`check_review_shape` before the
GitLab API call. When the gate refuses, the API call is never attempted
and no receipt DM (``notify_user_on_behalf_post``) fires for a blocked
post — short-circuited by the same ``(message, 1)`` return shape the
on-behalf gate uses.

Shape rules (post-#1159):

* **Own MR** (``mr.author == current_username``) — exempt. Own-MR
    reviews can be long-form (self-review summary, evidence block).
* **Colleague MR**, inline or MR-level — guarded by a **paragraph +
    word count** combination rather than a sentence count. Reject when
    the body has more than :data:`COLLEAGUE_PROSE_CAP_PARAGRAPHS`
    paragraphs (blank-line separated) or more than
    :data:`COLLEAGUE_PROSE_CAP_WORDS` words. The sentence-count
    heuristic (#1114) over-rejected legitimate ≤2-sentence findings
    that contained clauses split by semicolons or dashes, while the
    paragraph + word combination still catches the multi-section
    Problem/Fix/Verification abuse shape the previous gate was added
    to prevent.

The signature ``(api, encoded_repo, mr, body, inline)`` is
forge-neutral — future GitHub PR support is a single-method extension
(swap the GitLab GET for a GitHub one).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabHTTPClient

COLLEAGUE_PROSE_CAP_PARAGRAPHS = 3
COLLEAGUE_PROSE_CAP_WORDS = 200

_TTL_MR_AUTHOR = 300  # 5 minutes — mirrors `_TTL_USERNAME` shape


def fetch_mr_author(api: "GitLabHTTPClient", encoded_repo: str, mr: int) -> str:
    """Return the MR author's username, cached for 5 minutes per ``(repo, mr)``.

    Reuses the :class:`~teatree.backends.gitlab.api.GitLabHTTPClient`
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


def _count_paragraphs(body: str) -> int:
    """Count blank-line-separated paragraphs in ``body``.

    A paragraph is a run of one or more non-empty lines, separated from
    the next paragraph by one or more blank lines. Leading/trailing
    blank lines do not introduce extra paragraphs. Empty (or
    whitespace-only) input returns 0.
    """
    text = body.strip()
    if not text:
        return 0
    return sum(1 for chunk in text.split("\n\n") if chunk.strip())


def _count_words(body: str) -> int:
    """Count whitespace-separated tokens in ``body``."""
    return len(body.split())


# ast-grep-ignore: ac-django-no-complexity-suppressions
def check_review_shape(  # noqa: PLR0913 — gate entry-point; each kwarg is a documented gate input (MR coordinate + body + inline flag + the #126 override).
    *,
    api: "GitLabHTTPClient",
    encoded_repo: str,
    mr: int,
    body: str,
    inline: bool,
    allow_long_review: bool = False,
) -> str:
    """Return a non-empty steering error when the colleague-MR shape rule is breached.

    Returns ``""`` (proceed) when:

    * ``allow_long_review`` is set — the documented escape for a
        legitimately long-form colleague-MR review (the CLI surfaces this
        as ``--allow-long-review``, consistent with the sibling override
        pattern), OR
    * the MR is the current identity's own MR (carve-out), OR
    * the body fits both the paragraph cap
        (:data:`COLLEAGUE_PROSE_CAP_PARAGRAPHS`) and the word cap
        (:data:`COLLEAGUE_PROSE_CAP_WORDS`).

    The steering error names the concrete breach (paragraph count or
    word count) so the agent knows exactly what to tighten, and points
    at the inline ``Nit:`` form that satisfies the rule.
    """
    if allow_long_review:
        return ""
    if not body:
        return ""
    if not is_colleague_mr(api, encoded_repo, mr):
        return ""

    paragraph_count = _count_paragraphs(body)
    word_count = _count_words(body)

    if paragraph_count > COLLEAGUE_PROSE_CAP_PARAGRAPHS:
        return _steering_error(
            inline=inline,
            breach=f"{paragraph_count}-paragraph",
            cap=f"{COLLEAGUE_PROSE_CAP_PARAGRAPHS}-paragraph cap",
        )
    if word_count > COLLEAGUE_PROSE_CAP_WORDS:
        return _steering_error(
            inline=inline,
            breach=f"{word_count}-word",
            cap=f"{COLLEAGUE_PROSE_CAP_WORDS}-word cap",
        )
    return ""


def _steering_error(*, inline: bool, breach: str, cap: str) -> str:
    """Build the actionable refusal message.

    ``breach`` describes what triggered the refusal ("4-paragraph",
    "250-word"); ``cap`` names the cap that was exceeded
    ("3-paragraph cap", "200-word cap"). The ``inline`` flag picks the
    "inline note" vs "MR-level prose" surface name. Both forms point
    at the canonical ``t3 review post-comment ... --file ... --line ...``
    invocation, since that is the satisfying shape in either case.
    """
    surface = "inline note" if inline else "MR-level prose"
    return (
        f"Refusing colleague-MR on-behalf post: {breach} {surface} exceeds the "
        f"{cap}. Re-post as an inline Nit:-prefixed comment on the exact file:line "
        '(see skills/review § "Single terse inline nit"):\n'
        '  t3 review post-comment <repo> <mr> "Nit: ..." --file <path> --line <N>\n'
        "Or, if approving, run `t3 review approve <repo> <mr>` (no body needed)."
    )
