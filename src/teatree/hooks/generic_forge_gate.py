"""Policy for generic findings in forge payloads and local commit messages."""

from pathlib import Path
from typing import NamedTuple

from teatree.hooks import ai_signature_scan, forge_write_scope, publish_surface
from teatree.hooks.banned_term_registry import allowlist_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.leak_policy import LEAK, Surface, Verdict, Visibility, decide
from teatree.hooks.privacy_generic_detectors import GenericFinding, generic_findings


class GenericGateFinding(NamedTuple):
    category: str
    warn: bool


def _trailer_line_spans(payload: str) -> list[tuple[int, int]]:
    trailer_lines = {line for line, _, _ in ai_signature_scan.scan_text(payload)}
    spans: list[tuple[int, int]] = []
    offset = 0
    for line_number, line in enumerate(payload.splitlines(keepends=True), 1):
        if line_number in trailer_lines:
            spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _outside_trailer(finding: GenericFinding, trailer_spans: list[tuple[int, int]]) -> bool:
    return finding.category != "email" or not any(start <= finding.position < end for start, end in trailer_spans)


def generic_payload_finding(
    payload: str,
    command: str,
    cwd_repo: Path | None,
    *,
    nonpublic: bool,
    ai_signature_scanner_available: bool,
) -> GenericGateFinding | None:
    local_commit = publish_surface.command_targets_local_commit(command, cwd_repo)
    verdict = decide(
        LEAK,
        Visibility.NON_PUBLIC if nonpublic else Visibility.UNKNOWN,
        Surface.LOCAL_COMMIT if local_commit else Surface.DIFF,
    )
    if verdict is Verdict.ALLOW:
        return None

    try:
        allowed = allowlist_terms()
    except BannedTermsUnsetError:
        allowed = ()
    findings = generic_findings(payload, allowlist=allowed, forge_write=not local_commit)
    if not findings or not (local_commit or forge_write_scope.is_forge_write(command, cwd_repo)):
        return None

    if ai_signature_scanner_available:
        trailer_spans = _trailer_line_spans(payload)
        findings = [finding for finding in findings if _outside_trailer(finding, trailer_spans)]
    return GenericGateFinding(findings[0].category, verdict is Verdict.WARN) if findings else None
