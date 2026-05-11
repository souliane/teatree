"""Auto-labeling, duplicate detection, and triage for GitHub issues (see #49)."""

import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from itertools import combinations

from teatree.utils.run import run_allowed_to_fail

LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bug": ("bug", "error", "broken", "crash", "crashes", "fails", "failing", "regression"),
    "enhancement": ("feat", "feature", "add", "improve", "improvement", "support"),
    "documentation": ("docs", "doc", "documentation", "readme"),
    "architecture": ("refactor", "split", "merge", "restructure", "consolidate", "deduplicate"),
}


def infer_labels(title: str, body: str) -> list[str]:
    """Return labels whose keywords match the issue title or body (case-insensitive, word-boundary)."""
    text = f"{title} {body}".lower()
    matched: list[str] = []
    for label, keywords in LABEL_KEYWORDS.items():
        pattern = r"\b(" + "|".join(re.escape(kw) for kw in keywords) + r")\b"
        if re.search(pattern, text):
            matched.append(label)
    return matched


@dataclass(frozen=True)
class LabelSuggestion:
    number: int
    title: str
    labels: list[str]


class LabelSuggester:
    """Fetch unlabeled issues from a repo and infer labels via keyword matching."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def collect_suggestions(self) -> list[LabelSuggestion]:
        result = run_allowed_to_fail(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body,labels",
            ],
            expected_codes=None,
        )
        if result.returncode != 0:
            sys.stderr.write(f"gh issue list failed: {result.stderr.strip()}\n")
            return []

        issues = json.loads(result.stdout or "[]")
        suggestions: list[LabelSuggestion] = []
        for issue in issues:
            if issue.get("labels"):
                continue
            labels = infer_labels(issue.get("title", ""), issue.get("body", "") or "")
            if not labels:
                continue
            suggestions.append(LabelSuggestion(number=issue["number"], title=issue["title"], labels=labels))
        return suggestions

    def apply(self, suggestions: list[LabelSuggestion]) -> None:
        for suggestion in suggestions:
            run_allowed_to_fail(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(suggestion.number),
                    "--repo",
                    self.repo,
                    *[arg for label in suggestion.labels for arg in ("--add-label", label)],
                ],
                expected_codes=None,
            )


# Conventional-commit prefix: `type(scope)!:` with optional scope and breaking `!`.
_CONVENTIONAL_PREFIX = re.compile(r"^\s*[a-z]+(?:\([^)]+\))?!?:\s*", flags=re.IGNORECASE)
# Trailing PR/issue reference: " (#123)".
_PR_SUFFIX = re.compile(r"\s*\(#\d+\)\s*$")
# Leading bracket tag: "[WIP]", "[RFC]", etc.
_BRACKET_TAG = re.compile(r"^\s*\[[^\]]+\]\s*")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lower-case, strip conventional-commit prefix / PR suffix / bracket tags / punctuation."""
    text = title.lower()
    text = _BRACKET_TAG.sub("", text)
    text = _CONVENTIONAL_PREFIX.sub("", text)
    text = _PR_SUFFIX.sub("", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class DuplicateMatch:
    a_number: int
    b_number: int
    a_title: str
    b_title: str
    score: float


class DuplicateFinder:
    """Find potentially duplicate open issues by normalized-title similarity."""

    def __init__(self, repo: str, *, threshold: float = 0.75) -> None:
        self.repo = repo
        self.threshold = threshold

    def find(self) -> list[DuplicateMatch]:
        result = run_allowed_to_fail(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body,labels",
            ],
            expected_codes=None,
        )
        if result.returncode != 0:
            sys.stderr.write(f"gh issue list failed: {result.stderr.strip()}\n")
            return []

        issues = json.loads(result.stdout or "[]")
        normalized = [(issue["number"], issue["title"], normalize_title(issue["title"])) for issue in issues]

        matches: list[DuplicateMatch] = []
        for (num_a, title_a, norm_a), (num_b, title_b, norm_b) in combinations(normalized, 2):
            if not norm_a or not norm_b:
                continue
            score = SequenceMatcher(None, norm_a, norm_b).ratio()
            if score >= self.threshold:
                matches.append(
                    DuplicateMatch(
                        a_number=num_a,
                        b_number=num_b,
                        a_title=title_a,
                        b_title=title_b,
                        score=score,
                    )
                )
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches


_ISSUE_REF_IN_TITLE = re.compile(r"\(#(\d+)\)")


@dataclass(frozen=True)
class ResolvedIssue:
    issue_number: int
    issue_title: str
    pr_number: int
    pr_title: str

    @property
    def confidence(self) -> str:
        return "high" if f"#{self.issue_number})" in self.pr_title else "medium"


@dataclass(frozen=True)
class StaleIssue:
    issue_number: int
    issue_title: str
    days_inactive: int


class TriageScanner:
    """Find resolved-but-open issues and stale issues."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _fetch_open_issues(self) -> list[dict]:
        result = run_allowed_to_fail(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body,labels,updatedAt",
            ],
            expected_codes=None,
        )
        if result.returncode != 0:
            sys.stderr.write(f"gh issue list failed: {result.stderr.strip()}\n")
            return []
        return json.loads(result.stdout or "[]")

    def _fetch_merged_prs(self) -> list[dict]:
        result = run_allowed_to_fail(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "merged",
                "--limit",
                "200",
                "--json",
                "number,title,mergedAt",
            ],
            expected_codes=None,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout or "[]")

    def find_resolved(self) -> list[ResolvedIssue]:
        issues = self._fetch_open_issues()
        if not issues:
            return []
        prs = self._fetch_merged_prs()
        if not prs:
            return []

        issue_numbers = {i["number"] for i in issues}
        issue_by_number = {i["number"]: i for i in issues}

        resolved: list[ResolvedIssue] = []
        for pr in prs:
            for match in _ISSUE_REF_IN_TITLE.finditer(pr["title"]):
                ref_number = int(match.group(1))
                if ref_number in issue_numbers:
                    issue = issue_by_number[ref_number]
                    resolved.append(
                        ResolvedIssue(
                            issue_number=ref_number,
                            issue_title=issue["title"],
                            pr_number=pr["number"],
                            pr_title=pr["title"],
                        )
                    )
        resolved.sort(key=lambda r: r.issue_number)
        return resolved

    def close_resolved(self, resolved: list[ResolvedIssue]) -> None:
        for r in resolved:
            run_allowed_to_fail(
                [
                    "gh",
                    "issue",
                    "close",
                    str(r.issue_number),
                    "--repo",
                    self.repo,
                    "--comment",
                    f"Auto-closed: resolved by #{r.pr_number} ({r.pr_title}).",
                ],
                expected_codes=None,
            )

    def find_stale(self, *, days: int = 30) -> list[StaleIssue]:
        issues = self._fetch_open_issues()
        if not issues:
            return []

        now = datetime.now(tz=UTC)
        stale: list[StaleIssue] = []
        for issue in issues:
            if issue.get("labels"):
                continue
            updated_str = issue.get("updatedAt", "")
            if not updated_str:
                continue
            updated = datetime.fromisoformat(updated_str)
            inactive_days = (now - updated).days
            if inactive_days >= days:
                stale.append(
                    StaleIssue(
                        issue_number=issue["number"],
                        issue_title=issue["title"],
                        days_inactive=inactive_days,
                    )
                )
        stale.sort(key=lambda s: s.days_inactive, reverse=True)
        return stale
