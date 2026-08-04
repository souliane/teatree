"""Auto-labeling, duplicate detection, and triage for GitHub issues (see #49).

Every verdict these scanners produce is a claim about a set they ENUMERATED, so a
failed enumeration is not an empty one. Each ``gh`` read therefore raises
:class:`ForgeEnumerationError` rather than degrading to ``[]`` — the fail-loud rule
in ``skills/rules/SKILL.md`` § "External Read Failure Must Fail Loud, Never
Silent-Empty". Returning ``[]`` on a failed read is what let ``t3 tool
triage-issues`` report a clean sweep from a scan that never ran while a hand triage
at the same moment found three resolved-but-open issues (#4135).
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from itertools import combinations

from teatree.utils.run import run_allowed_to_fail


class ForgeEnumerationError(RuntimeError):
    """A forge read a verdict depends on did not run — the answer is UNKNOWN, not empty."""


def _gh_json(args: list[str], *, what: str) -> list[dict]:
    """Run a ``gh`` list command and parse its JSON, or raise.

    The single chokepoint every enumeration here goes through, so no caller can
    reintroduce the silent-empty by hand.
    """
    result = run_allowed_to_fail(["gh", *args], expected_codes=None)
    if result.returncode != 0:
        msg = f"{what} failed: {result.stderr.strip()}"
        raise ForgeEnumerationError(msg)
    return json.loads(result.stdout or "[]")


#: The fields every open-issue enumeration here reads.
_OPEN_ISSUE_FIELDS = "number,title,body,labels,updatedAt"


def _open_issues(repo: str) -> list[dict]:
    """Every open issue in *repo*, or raise :class:`ForgeEnumerationError`."""
    return _gh_json(
        ["issue", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", _OPEN_ISSUE_FIELDS],
        what="gh issue list",
    )


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
        issues = _open_issues(self.repo)
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
        issues = _open_issues(self.repo)
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


# Matches a ``#N`` issue reference anywhere in a PR title — the canonical
# parenthesized ``(#N)`` form *and* loose mentions like "fixes #N". The
# parenthesized form drives the high-confidence verdict (see
# ``ResolvedIssue.confidence``).
_ISSUE_REF_IN_TITLE = re.compile(r"(?<![\w/])#(\d+)\b")


@dataclass(frozen=True)
class ResolvedIssue:
    issue_number: int
    issue_title: str
    pr_number: int
    pr_title: str

    @property
    def confidence(self) -> str:
        canonical = f"(#{self.issue_number})"
        return "high" if canonical in self.pr_title else "medium"


@dataclass(frozen=True)
class StaleIssue:
    issue_number: int
    issue_title: str
    days_inactive: int


class TriageScanner:
    """Find resolved-but-open issues and stale issues."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _fetch_merged_prs(self) -> list[dict]:
        return _gh_json(
            [
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
            what="gh pr list",
        )

    def find_resolved(self) -> list[ResolvedIssue]:
        issues = _open_issues(self.repo)
        prs = self._fetch_merged_prs()
        if not issues or not prs:
            return []

        issue_numbers = {i["number"] for i in issues}
        issue_by_number = {i["number"]: i for i in issues}

        resolved: list[ResolvedIssue] = []
        for pr in prs:
            ref_numbers = {int(m.group(1)) for m in _ISSUE_REF_IN_TITLE.finditer(pr["title"])}
            for ref_number in sorted(ref_numbers & issue_numbers):
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
        issues = _open_issues(self.repo)
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
