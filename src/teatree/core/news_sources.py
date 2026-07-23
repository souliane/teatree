"""The merged press-review source table (#3669).

The news loop scanned two AI newsletters and asked one distinguishing question of
each story: *is there a concrete change to teatree this suggests?* A press-review
aggregator running elsewhere scanned a much wider feed set — web/devops/infosec/
python newsletters plus Hacker News — with URL-normalised dedupe against a
rolling seen-set.

Both are kept. The wider feed set is MERGED IN for breadth; the teatree-relevance
judgement stays exactly where it was (the ``scanning-news`` skill's triage step),
because that filter is the loop's whole value — breadth without it is a reading
list, not an improvement signal.

Dedupe is by NORMALISED feed URL and by label, first entry wins, so an overlapping
source keeps the existing loop's row: those two carry ``edition_dated``, the
date-of-edition gate the skill enforces, which the aggregator's plain RSS row does
not. The rolling seen-set the aggregator kept in a JSON file has a durable
equivalent here already — ``PendingArticleSuggestion.record_candidate`` is
idempotent by source URL — so no second store is introduced.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

#: Query parameters that identify the referrer, not the resource — stripped before
#: two spellings of one URL are compared.
_TRACKING_PARAMS = frozenset(
    {
        "__twitter_impression",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "ref_url",
        "share",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)

_SCHEME_RE = re.compile(r"^https?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NewsSource:
    """One feed the scan reads.

    *edition_dated* marks a source published as a dated edition rather than a
    rolling feed — the skill's mandatory date-of-edition verification applies to
    those and only those.
    """

    bucket: str
    label: str
    url: str
    max_items: int
    edition_dated: bool = False


def normalize_source_url(url: str) -> str:
    """Canonical spelling of *url* for dedupe — no tracking params, no fragment.

    Scheme and host lowercase (case-insensitive by spec); the path's case is
    preserved (it is not). A value that does not parse as an HTTP(S) URL passes
    through untouched rather than being mangled into a false match.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    if not parsed.netloc or not _SCHEME_RE.match(parsed.scheme):
        return url
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key.lower() not in _TRACKING_PARAMS]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), ""))


def merge_sources(*groups: Sequence[NewsSource]) -> tuple[NewsSource, ...]:
    """Concatenate *groups*, dropping a later source that repeats a label or feed URL."""
    seen_labels: set[str] = set()
    seen_urls: set[str] = set()
    merged: list[NewsSource] = []
    for group in groups:
        for source in group:
            key = normalize_source_url(source.url)
            if source.label in seen_labels or key in seen_urls:
                continue
            seen_labels.add(source.label)
            seen_urls.add(key)
            merged.append(source)
    return tuple(merged)


#: The news loop's own sources — the two edition-dated AI newsletters the
#: teatree-relevance triage was built around.
EXISTING_SOURCES: tuple[NewsSource, ...] = (
    NewsSource(bucket="ai", label="TLDR AI", url="https://tldr.tech/ai/", max_items=15, edition_dated=True),
    NewsSource(
        bucket="ai",
        label="The Rundown AI",
        url="https://www.therundown.ai/archive",
        max_items=15,
        edition_dated=True,
    ),
)

#: The breadth the press-review aggregator contributes. Its own TLDR AI / Rundown
#: rows are the RSS spellings of the two above and are deduped away by
#: :func:`merge_sources`, which keeps the edition-dated rows.
PRESS_REVIEW_SOURCES: tuple[NewsSource, ...] = (
    NewsSource(bucket="aggregator", label="Hacker News", url="https://news.ycombinator.com/best", max_items=25),
    NewsSource(bucket="ai", label="TLDR AI", url="https://tldr.tech/api/rss/ai", max_items=15),
    NewsSource(bucket="ai", label="The Rundown AI", url="https://rss.beehiiv.com/feeds/2R3C6Bt5wj.xml", max_items=15),
    NewsSource(bucket="ai", label="AI Jungle", url="https://theaijungle.substack.com/feed", max_items=15),
    NewsSource(bucket="webdev", label="TLDR Web Dev", url="https://tldr.tech/api/rss/webdev", max_items=15),
    NewsSource(bucket="devops", label="TLDR DevOps", url="https://tldr.tech/api/rss/devops", max_items=10),
    NewsSource(bucket="infosec", label="TLDR InfoSec", url="https://tldr.tech/api/rss/infosec", max_items=10),
    NewsSource(
        bucket="industry",
        label="Pragmatic Engineer",
        url="https://newsletter.pragmaticengineer.com/feed",
        max_items=6,
    ),
    NewsSource(bucket="python", label="PyCoder's Weekly", url="https://pycoders.com/feed", max_items=8),
    NewsSource(bucket="python", label="Django News", url="https://django-news.com/issues.rss", max_items=6),
    NewsSource(bucket="python", label="Real Python", url="https://realpython.com/atom.xml", max_items=5),
    NewsSource(bucket="python", label="PSF Blog", url="https://blog.python.org/feeds/posts/default", max_items=4),
)

#: The table the scan reads.
NEWS_SOURCES: tuple[NewsSource, ...] = merge_sources(EXISTING_SOURCES, PRESS_REVIEW_SOURCES)


def render_source_directive(sources: Iterable[NewsSource]) -> str:
    """The source list stamped into a scanning-news task's dispatch directive.

    The agent runs shell-denied and fetches by URL, so the merged table has to
    reach it as text rather than as an import. Each line names the bucket, the
    label, the URL, and the item cap; an edition-dated source is marked so the
    skill's date gate is applied to it and not to a rolling feed.
    """
    lines = ["SOURCES (fetch every one; teatree-relevance triage still decides what survives):"]
    lines.extend(
        f"- [{source.bucket}] {source.label} — {source.url} (max {source.max_items}"
        + (", edition-dated: verify the issue date)" if source.edition_dated else ")")
        for source in sources
    )
    return "\n".join(lines)


__all__ = [
    "EXISTING_SOURCES",
    "NEWS_SOURCES",
    "PRESS_REVIEW_SOURCES",
    "NewsSource",
    "merge_sources",
    "normalize_source_url",
    "render_source_directive",
]
