r"""Own-repo-URL carve-out for the banned-terms posting gate (#1415).

Sibling of the email carve-out (``term_match.strip_emails``) and the own-slug
commit downgrade (``_commit_carve_out.own_slug_term_downgrades``). It closes a
distinct false positive: a publish to a PUBLIC surface (a public teatree
issue/PR/comment) whose body legitimately cites the *address of a customer
repo* — a forge work-item URL like
``https://gitlab.com/<org>-engineering/<repo>/-/issues/N``. That URL is the
structurally-required location of the customer's own work item, not a leak; yet
the org/namespace token inside it trips the banned-terms scan.

The carve-out is DERIVED, never hardcoded: a URL is "own-repo" only when its
host+namespace slug matches one of the active overlay's configured
``[teatree] private_repos`` entries (the same allowlist the commit / pure-post
carve-out already consults), via the host-qualification-symmetric
``slug_namespace_matches``. A foreign URL, a URL under no configured namespace,
and a bare term occurrence OUTSIDE any own-repo URL all keep the hard block.

Forge-URL slug parsing routes through :func:`teatree.utils.url_slug.slug_from_issue_or_pr_url`
(the canonical path-grammar home), so this module never re-derives the
``/-/issues`` / ``/pull`` path literals. The predicate is fail-safe-to-block: it
returns True ONLY when the term is present in the payload AND every one of its
occurrences disappears once the own-repo URLs are blanked. A no-allowlist config,
a foreign-URL occurrence, a bare occurrence, or a config read error all yield
False, so the gate keeps denying — a detection failure never weakens the gate.
"""

import re
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from teatree.hooks._repo_visibility import _private_repo_allowlist, slug_namespace_matches
from teatree.hooks.term_match import matched_term
from teatree.utils.url_slug import slug_from_issue_or_pr_url

# A bare http(s) URL token carrying NO forge path-shape grammar (that grammar
# lives only in teatree.utils.url_slug); host+slug are resolved per-URL below.
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[^\s)\]>\"']+")


def _url_is_own_repo(url: str, allowlist: list[str]) -> bool:
    """Whether ``url`` is a forge work-item URL under a configured private repo.

    The slug (``owner/repo`` / ``group/.../repo``) is parsed through the
    canonical :func:`slug_from_issue_or_pr_url`; the host segment is prepended so
    a host-qualified allowlist entry matches. A non-work-item URL (no parseable
    slug) is never own-repo.
    """
    parsed = urlparse(url.rstrip(".,;"))
    slug = slug_from_issue_or_pr_url(parsed.path)
    if not slug:
        return False
    host = (parsed.hostname or "").lower()
    qualified = f"{host}/{slug}".lower()
    return any(slug_namespace_matches(entry, qualified) for entry in allowlist)


def _blank_own_repo_urls(text: str, allowlist: list[str]) -> str:
    """Replace every own-repo forge URL in ``text`` with a single space.

    Mirrors :func:`term_match.strip_emails`: an own-repo URL is the address of
    the overlay's own customer repo, so blanking it before matching removes the
    namespace token an own-repo URL legitimately carries while leaving every
    other occurrence (a bare term, a foreign URL) intact for the matcher.
    """
    return _URL_RE.sub(lambda m: " " if _url_is_own_repo(m.group(0), allowlist) else m.group(0), text)


def term_only_inside_own_repo_urls(payload: str, term: str, *, config_path: Path | None = None) -> bool:
    """Return True iff every occurrence of ``term`` in ``payload`` sits inside an own-repo URL.

    An own-repo URL is a forge work-item URL whose host+namespace slug matches a
    ``[teatree] private_repos`` allowlist entry (host-qualification-symmetric).
    The term must be PRESENT in the payload (the matcher fires on the raw text)
    and ABSENT once every own-repo URL is blanked — only then is the term
    confined to addresses of the overlay's own repos and the gate may downgrade
    to a warning. A bare occurrence, a foreign-URL occurrence, an empty
    allowlist, or a config read error all yield False so the hard block stands.
    """
    if not payload or not term:
        return False
    terms = (term,)
    if matched_term(payload, terms) is None:
        return False
    allowlist = _private_repo_allowlist(config_path)
    if not allowlist:
        return False
    blanked = _blank_own_repo_urls(payload, allowlist)
    return matched_term(blanked, terms) is None
