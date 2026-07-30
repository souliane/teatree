"""Pre-publish scanner that strips ``Closes/Fixes/Resolves`` trailers (#1398).

The user-scoped setting ``ban_close_trailers_on_namespaces`` lists fnmatch
patterns over ``namespace/repo``. When the target PR/MR's repo matches one of
the patterns and the body carries an auto-close trailer, the trailer line is
silently stripped before publishing. DB-home (#1775); set via ``t3 <overlay>
config_setting set ban_close_trailers_on_namespaces``; the TOML value is ignored
on read.

Distinct from the overlay-scoped ``forbid_close_keywords`` gate (#1012)
which raises ``SystemExit`` to refuse the publish: this scanner cleans
the body and lets the publish proceed.

Default config (absent setting / empty list) is a no-op.
"""

import fnmatch
import re
from urllib.parse import urlparse

CLOSE_TRAILER_RE = re.compile(
    r"^(?P<kw>close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?P<part>\s+part\s+of)?"
    r"(?::\s*|\s+)"
    r"(?P<ref>(?:[\w./-]+)?#\d+|https?://\S+)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_close_trailers(body: str) -> str:
    """Remove every line that starts with a ``Closes|Fixes|Resolves`` trailer.

    Case-insensitive. Matches the ``part of`` variant and both ``#N`` and
    full-URL reference forms. Trailing blank lines exposed by the removal
    are also stripped so the cleaned body has no dangling whitespace.
    """
    if not body:
        return body
    cleaned_lines = [line for line in body.splitlines() if not CLOSE_TRAILER_RE.match(line)]
    cleaned = "\n".join(cleaned_lines)
    return cleaned.rstrip()


def _normalise_repo(repo: str) -> str:
    """Reduce a repo identifier to its ``namespace/repo`` form.

    Accepts raw ``namespace/repo`` strings, full HTTPS URLs, and paths
    with extra trailing segments. fnmatch patterns are evaluated against
    this normalised form so a ``"eng-group/*"`` pattern matches both
    ``eng-group/product`` and ``https://gitlab.com/eng-group/product``.
    """
    if repo.startswith(("http://", "https://")):
        parsed = urlparse(repo)
        return parsed.path.lstrip("/")
    return repo


def namespace_is_banned(repo: str, patterns: list[str]) -> bool:
    """True when *repo* matches any fnmatch *pattern* on its leading segments.

    Matches both an exact ``namespace/repo`` and the leading-namespace
    form: a pattern of ``"eng-group/*"`` matches ``"eng-group/product"``
    and ``"eng-group/sub/product"`` because the latter's leading
    ``"eng-group/sub"`` is still under the banned namespace.
    """
    if not patterns:
        return False
    normalised = _normalise_repo(repo)
    parts = normalised.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    candidates.append(normalised)
    for pattern in patterns:
        for candidate in candidates:
            if fnmatch.fnmatchcase(candidate, pattern):
                return True
    return False


def apply_publish_gate(body: str, *, repo: str, patterns: list[str]) -> str:
    """Strip close trailers when *repo* matches a banned namespace pattern.

    Returns the cleaned body, or *body* unchanged when the namespace
    is not banned (or *patterns* is empty). Idempotent — running the
    scanner over an already-clean body returns the same body.
    """
    if not namespace_is_banned(repo, patterns):
        return body
    return strip_close_trailers(body)
