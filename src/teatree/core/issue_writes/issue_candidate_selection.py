"""Fresh open-issue listing and deterministic draft-relative candidate selection."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from teatree.core.backend_protocols import CodeHostBackend
from teatree.types import RawAPIDict

_WORDS = re.compile(r"\w+")
ISSUE_CREATE_CANDIDATE_LIMIT = 20


@dataclass(frozen=True)
class OpenCandidates:
    all_open: dict[str, RawAPIDict]
    closest: dict[str, RawAPIDict]


def description_of(issue: RawAPIDict) -> str:
    """GitLab uses description; GitHub uses body."""
    for key in ("description", "body"):
        value = issue.get(key)
        if isinstance(value, str):
            return value
    return ""


def issue_url_of(issue: RawAPIDict) -> str:
    """GitLab uses web_url; GitHub uses html_url."""
    for key in ("web_url", "html_url", "url"):
        value = issue.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def open_candidates(*, host: CodeHostBackend, repo: str) -> dict[str, RawAPIDict]:
    """Read fresh; ticket-scoped LandscapeArtifact may be stale."""
    return {url: issue for issue in host.list_repo_open_issues(repo=repo) if (url := issue_url_of(issue))}


def _tokens(text: str) -> set[str]:
    return set(_WORDS.findall(text.casefold()))


def _overlap(first: set[str], second: set[str]) -> float:
    return len(first & second) / len(first | second) if first and second else 0.0


def select_closest(
    live: Mapping[str, RawAPIDict],
    title: str,
    body: str,
    *,
    limit: int = ISSUE_CREATE_CANDIDATE_LIMIT,
) -> dict[str, RawAPIDict]:
    """Rank by title and description similarity, breaking equal scores by URL."""
    title_words, body_words = _tokens(title), _tokens(body)

    def score(issue: RawAPIDict) -> float:
        raw_title = issue.get("title")
        issue_title = _tokens(raw_title if isinstance(raw_title, str) else "")
        issue_body = _tokens(description_of(issue))
        return (
            4 * _overlap(title_words, issue_title)
            + 2 * _overlap(title_words, issue_body)
            + 2 * _overlap(body_words, issue_title)
            + _overlap(body_words, issue_body)
        )

    urls = sorted(live, key=lambda url: (-score(live[url]), url))[:limit]
    return {url: live[url] for url in urls}


def closest_open_candidates(*, host: CodeHostBackend, repo: str, title: str, body: str) -> OpenCandidates:
    all_open = open_candidates(host=host, repo=repo)
    return OpenCandidates(all_open=all_open, closest=select_closest(all_open, title, body))
