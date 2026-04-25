"""Auto-labeling and duplicate detection for GitHub issues — triage tool (see #49)."""

import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations

from teatree.utils.run import run_allowed_to_fail

LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bug": ("bug", "error", "broken", "crash", "crashes", "fails", "failing", "regression"),
    "enhancement": ("feat", "feature", "add", "improve", "improvement", "support"),
    "documentation": ("docs", "doc", "documentation", "readme"),
    "architecture": ("refactor", "split", "merge", "restructure", "consolidate", "deduplicate"),
    "dashboard": ("dashboard", "panel", "view", "widget"),
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
