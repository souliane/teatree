import operator
import re
from difflib import SequenceMatcher

_LABEL_PATTERNS: dict[str, list[str]] = {
    "bug": [r"\b(bug|error|broken|crash|fix|regression)\b"],
    "enhancement": [r"\b(feat|add|improve|enhance|support)\b"],
    "documentation": [r"\b(doc|readme|guide|tutorial)\b"],
    "investigation": [r"\b(investigate|explore|evaluate|research|spike)\b"],
    "infra": [r"\b(ci|cd|pipeline|docker|deploy|infra)\b"],
}


def find_duplicates(title: str, existing_titles: list[str], *, threshold: float = 0.7) -> list[tuple[str, float]]:
    normalized = title.lower().strip()
    matches = [
        (existing, SequenceMatcher(None, normalized, existing.lower().strip()).ratio()) for existing in existing_titles
    ]
    return sorted(
        [(t, s) for t, s in matches if s >= threshold],
        key=operator.itemgetter(1),
        reverse=True,
    )


def suggest_labels(title: str, body: str = "") -> list[str]:
    text = f"{title} {body}".lower()
    return [
        label for label, patterns in _LABEL_PATTERNS.items() if any(re.search(p, text, re.IGNORECASE) for p in patterns)
    ]
