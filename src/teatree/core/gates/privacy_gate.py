"""Pre-publish privacy gate (#1295 capability J).

Sibling of ``_close_keyword_gate.py``: every public-repo write path
(``gh pr create``, ``gh pr edit``, ``gh issue create``, commit pump,
release notes, sub-agent prompts targeting a public repo) consults the
gate before the network call. The gate scans the candidate text for
patterns the active overlay marks as private (customer-domain acronyms,
internal org prefixes, quote anchors) and refuses with a structured
error when any match fires.

The gate is *public-target-aware*: it never fires for writes to a repo
that is NOT in :attr:`OverlayConfig.public_repos`. A bypass flag
``--privacy-ok`` (or the kwarg ``bypass=True`` on
:func:`scan_for_publication`) authorises an intentional publish.
"""

import logging
import re
from dataclasses import dataclass

from teatree.hooks import term_match

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrivacyMatch:
    """One match the gate surfaces back to the caller."""

    pattern_name: str
    matched_text: str
    position: int


@dataclass(frozen=True, slots=True)
class PrivacyGateResult:
    """Verdict from :func:`scan_for_publication` — refused if matches present."""

    target_repo: str
    is_public: bool
    matches: tuple[PrivacyMatch, ...] = ()

    @property
    def refused(self) -> bool:
        """The gate refuses when the target is public AND any pattern matched."""
        return self.is_public and bool(self.matches)


# Default block patterns that apply regardless of overlay configuration
# (recurrence-#3 enforcement gap from feedback_redcard_never_quote_user_on_public_repos):
# - Markdown blockquotes carrying first-person markers
# - Verbatim quotation anchors
_DEFAULT_QUOTE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "blockquote_first_person",
        r"^>\s+.*\b(I|my|me|user said|verbatim|User mandate)\b",
    ),
    (
        "verbatim_anchor",
        r"\b(verbatim|user said|User mandate \(verbatim)\b",
    ),
)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def scan_for_publication(  # noqa: PLR0913 — gate entry-point; each kwarg is a documented input.
    *,
    text: str,
    target_repo: str,
    public_repos: list[str],
    redact_terms: list[str] | None = None,
    block_patterns: list[str] | None = None,
    bypass: bool = False,
) -> PrivacyGateResult:
    """Scan *text* against the active overlay's privacy rules.

    Returns a :class:`PrivacyGateResult` whose :attr:`refused` flag is
    ``True`` when the target is public and at least one pattern matched.
    Bypass short-circuits to a clean result (no matches surfaced) so
    intentional publishes are not noisy.
    """
    is_public = target_repo in public_repos
    if not is_public or bypass:
        return PrivacyGateResult(target_repo=target_repo, is_public=is_public)
    matches: list[PrivacyMatch] = []
    for term in redact_terms or []:
        if not term:
            continue
        # Whole-token matching via the SHARED matcher (the same one every other
        # banned-terms/overlay-leak gate uses), NOT substring ``re.escape`` —
        # so a short redact term no longer surfaces inside a longer word
        # (``op`` inside ``cooperative``) while a camelCase/snake split token
        # still matches.
        for matched_text, position in term_match.iter_term_matches(text, term):
            matches.append(
                PrivacyMatch(
                    pattern_name=f"redact:{term}",
                    matched_text=matched_text,
                    position=position,
                ),
            )
    pattern_sources: list[tuple[str, str]] = list(_DEFAULT_QUOTE_PATTERNS)
    pattern_sources.extend((f"block:{p}", p) for p in (block_patterns or []) if p)
    for name, pattern in pattern_sources:
        try:
            compiled = re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            # Fail closed: a rule we cannot evaluate must block the publish, never
            # silently pass. Surface it as a match so the gate refuses.
            logger.warning("privacy gate: unusable block pattern %r (%s) — treating as blocking", pattern, exc)
            matches.append(
                PrivacyMatch(
                    pattern_name=f"{name}:invalid",
                    matched_text=pattern,
                    position=0,
                ),
            )
            continue
        for match in compiled.finditer(text):
            matches.append(  # noqa: PERF401
                PrivacyMatch(
                    pattern_name=name,
                    matched_text=match.group(0),
                    position=match.start(),
                ),
            )
    return PrivacyGateResult(
        target_repo=target_repo,
        is_public=True,
        matches=tuple(matches),
    )


def format_refusal(result: PrivacyGateResult) -> str:
    """Render the structured error message the gate returns to the caller."""
    lines = [
        f"privacy gate refused: {result.target_repo} is in `public_repos`, found {len(result.matches)} matches:",
    ]
    lines.extend(
        f"  - {match.pattern_name} at position {match.position}: {match.matched_text!r}" for match in result.matches
    )
    lines.append("Re-run with `--privacy-ok` only when the matches are intentional.")
    return "\n".join(lines)


def _overlay_publication_rules() -> tuple[list[str], list[str], list[str]]:
    """The active overlay's ``(public_repos, privacy_redact_terms, privacy_block_patterns)``.

    Best-effort: when no overlay resolves — none installed, Django not yet set
    up, or several with no ``T3_OVERLAY_NAME`` to disambiguate — every list is
    empty, so :func:`scan_outbound_text` refuses nothing (the gate only ever
    fires on a public-repo target the overlay itself declared). Resolution is
    wrapped so a publish never crashes on a missing/partial overlay, mirroring
    the same egress-never-crash posture as ``reply_transport._linkify_for_slack``.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred Django import.

    try:
        config = get_overlay().config
        return list(config.public_repos), list(config.privacy_redact_terms), list(config.privacy_block_patterns)
    except Exception as exc:  # noqa: BLE001 — overlay resolution is best-effort; a publish must never crash on it.
        logger.debug("publication privacy gate: overlay rules unresolved (%s) — scanning nothing", exc)
        return [], [], []


def scan_outbound_text(*, text: str, target_repo: str) -> PrivacyGateResult:
    """Scan outbound *text* bound for *target_repo* against the active overlay's rules.

    The egress-chokepoint wrapper of :func:`scan_for_publication`: it resolves
    the active overlay's ``public_repos`` plus redact/quote rules and delegates,
    so a publication seam need only supply the body and its repo target. The
    scan self-gates on ``public_repos`` — a non-repo destination (a Slack
    channel ref) or a private repo is never in it, so the scan refuses nothing
    there.
    """
    public_repos, redact_terms, block_patterns = _overlay_publication_rules()
    return scan_for_publication(
        text=text,
        target_repo=target_repo,
        public_repos=public_repos,
        redact_terms=redact_terms,
        block_patterns=block_patterns,
    )
