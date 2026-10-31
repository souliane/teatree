"""The one scan a text crosses before it is published, shared by the publish command and the merge gate."""

import re
from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import StrEnum

from teatree.core.gates.privacy_gate import BUILTIN_QUOTE_PATTERN_NAMES, format_refusal, scan_outbound_text
from teatree.core.publishing.close_keyword import CLOSE_KEYWORD_RE
from teatree.hooks import quote_scanner
from teatree.hooks.ai_signature_scan import scan_text, summary

_ISSUE_URL_RE = re.compile(r"^https?://[^/]+/(?P<slug>.+?)/(?:-/)?(?:issues|work_items)/(?P<number>\d+)", re.IGNORECASE)


class FindingKind(StrEnum):
    AI_SIGNATURE = "ai-signature"
    LEAK = "leak"
    USER_QUOTE = "user-quote"
    CLOSING_KEYWORD = "closing-keyword"

    @property
    def headline(self) -> str:
        return _HEADLINES[self]


_HEADLINES: dict[FindingKind, str] = {
    FindingKind.AI_SIGNATURE: "carries an AI-signature trailer",
    FindingKind.LEAK: "fails the public-repo leak scan",
    FindingKind.USER_QUOTE: "carries a user quote",
    FindingKind.CLOSING_KEYWORD: "carries a closing keyword",
}


@dataclass(frozen=True, slots=True)
class LiveTextFinding:
    kind: FindingKind
    detail: str


@dataclass(frozen=True, slots=True)
class LiveTextVerdict:
    findings: tuple[LiveTextFinding, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings

    def render(self) -> str:
        return "\n".join(f"{finding.kind.headline}\n{finding.detail}" for finding in self.findings)


def canonical_ref(ref: str, repo: str) -> str:
    """``slug#N`` for a ref spelled ``#N``, ``slug#N`` or an issue URL, case-folded."""
    if match := _ISSUE_URL_RE.match(ref):
        return f"{match['slug']}#{match['number']}".casefold()
    return f"{repo}{ref}".casefold() if ref.startswith("#") else ref.casefold()


def scan_live_text(text: str, repo: str, forge: str, allowed_close_refs: Collection[str] = ()) -> LiveTextVerdict:
    """Every finding that stops *text* being published to *repo*; a closing keyword passes only for an allowed ref."""
    findings = [
        *_signature_findings(text),
        *_public_target_findings(text, repo, forge),
        *_closing_keyword_findings(text, repo, allowed_close_refs),
    ]
    return LiveTextVerdict(tuple(findings))


def _signature_findings(text: str) -> list[LiveTextFinding]:
    return [LiveTextFinding(FindingKind.AI_SIGNATURE, summary(found))] if (found := scan_text(text)) else []


def _public_target_findings(text: str, repo: str, forge: str) -> list[LiveTextFinding]:
    privacy = scan_outbound_text(text=text, target_repo=repo, forge=forge)
    if not privacy.is_public:
        return []
    leaks = replace(
        privacy, matches=tuple(m for m in privacy.matches if m.pattern_name not in BUILTIN_QUOTE_PATTERN_NAMES)
    )
    findings = [LiveTextFinding(FindingKind.LEAK, format_refusal(leaks))] if leaks.refused else []
    if quotes := quote_scanner.scan_text(text).high:
        findings.append(LiveTextFinding(FindingKind.USER_QUOTE, ", ".join(sorted({q.name for q in quotes}))))
    return findings


def _closing_keyword_findings(text: str, repo: str, allowed: Collection[str]) -> list[LiveTextFinding]:
    permitted = {canonical_ref(ref, repo) for ref in allowed}
    return [
        LiveTextFinding(FindingKind.CLOSING_KEYWORD, f"`{match.group(0)}` would auto-close {match['ref']} on merge")
        for match in CLOSE_KEYWORD_RE.finditer(text)
        if canonical_ref(match["ref"], repo) not in permitted
    ]
