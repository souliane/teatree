"""Auto-labeling for GitHub issues — first slice of the triage tool (see #49)."""

import json
import re
import sys
from dataclasses import dataclass

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
