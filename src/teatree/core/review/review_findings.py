"""Deterministic scaffold for the ``retro review-findings`` command (#1573).

The retro skill (``skills/retro/SKILL.md`` § "Recurrence → Escalation")
already prescribes classifying each review finding as **A** (a gate or test
already enforces it), **B** (genuinely one-off), or **C** (a recurring class
with no enforcement). That classification was prose-only — nothing recorded
the verdicts or filed the tracking issue, so class-C findings kept recurring.

This module is the reliable, deterministic half: it fetches a PR's review
comments through the existing :class:`~teatree.core.backend_protocols.CodeHostBackend`,
computes a stable fingerprint per finding for dedup, records each finding +
its supplied classification to a durable per-PR JSON store, and — for
class-C findings only — files a scoped enforcement issue via the same backend
the agent files issues through, deduped against already-filed issues by
fingerprint marker.

The A/B/C judgement itself is *supplied* to the command (a fingerprint→class
mapping the agent computes after reading the diff and the existing gates); the
scaffold never guesses it. A heuristic-only auto-classifier would be unreliable
— "is this already enforced?" needs reading the gate set, and "is this
recurring?" needs cross-retro recall — so the verdict is an input, not a guess.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from teatree.core.models import NEEDS_TRIAGE_LABEL
from teatree.hooks import banned_terms_scanner
from teatree.paths import get_data_dir
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

_FINGERPRINT_MARKER = "retro-finding-fingerprint:"
"""Prefix of the hidden marker line embedded in every filed enforcement issue.

Dedup reads this back: a re-run searches open issues for the fingerprint and
skips filing when a marker already matches, so re-running never refiles.
"""

_WHITESPACE_RE = re.compile(r"\s+")


class FindingClass(StrEnum):
    """A retro finding's enforcement classification (retro SKILL.md § Recurrence).

    ``A`` — a gate or test already enforces this; no new artifact needed.
    ``B`` — genuinely one-off; no enforcement warranted.
    ``C`` — a recurring class with no enforcement; needs the smallest
    structural gate/test/hook, tracked by a filed enforcement issue.
    """

    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One review comment fetched from a PR, plus its stable fingerprint."""

    body: str
    path: str
    line: int
    author: str

    @property
    def fingerprint(self) -> str:
        """A stable hash of the normalized body + file + line.

        Normalizing collapses whitespace and lowercases so cosmetic edits to
        the comment (re-wrapping, trailing spaces) don't defeat dedup, while
        a genuinely different finding on a different line still hashes apart.
        """
        normalized = _WHITESPACE_RE.sub(" ", self.body).strip().lower()
        seed = f"{normalized}\x1f{self.path}\x1f{self.line}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ClassifiedFinding:
    """A finding paired with the agent-supplied A/B/C verdict."""

    finding: ReviewFinding
    classification: FindingClass


@dataclass(frozen=True, slots=True)
class FiledIssue:
    """Outcome of handling one class-C finding.

    ``url`` is the filed (or already-filed) issue link. ``withheld`` is set
    when the rendered body would leak a banned term (a customer/tenant name)
    even after reference-neutralization — the issue is NOT filed, so nothing
    leaks, and ``withheld_reason`` records why for the summary.
    """

    fingerprint: str
    url: str
    already_filed: bool
    withheld: bool = False
    withheld_reason: str = ""


@dataclass(frozen=True, slots=True)
class ReviewFindingsSummary:
    """The command's machine-readable result: per-class counts + filed links."""

    pr_url: str
    counts: dict[str, int]
    filed: list[FiledIssue]

    def as_dict(self) -> RawAPIDict:
        return {
            "pr_url": self.pr_url,
            "counts": self.counts,
            "filed": [
                {
                    "fingerprint": f.fingerprint,
                    "url": f.url,
                    "already_filed": f.already_filed,
                    "withheld": f.withheld,
                    "withheld_reason": f.withheld_reason,
                }
                for f in self.filed
            ],
        }


def parse_findings(comments: list[RawAPIDict]) -> list[ReviewFinding]:
    """Build :class:`ReviewFinding` rows from raw forge comment payloads.

    Handles both GitHub PR review comments (``path`` + ``line``) and
    issue-level PR comments (no ``path``); a comment with an empty body is
    dropped because a fingerprint over an empty body is meaningless.
    """
    findings: list[ReviewFinding] = []
    for raw in comments:
        body = str(raw.get("body") or "").strip()
        if not body:
            continue
        findings.append(
            ReviewFinding(
                body=body,
                path=str(raw.get("path") or ""),
                line=_coerce_line(raw),
                author=_comment_author(raw),
            )
        )
    return findings


def _coerce_line(raw: RawAPIDict) -> int:
    """Best-effort line number from a forge comment (``line`` or ``position``)."""
    for key in ("line", "original_line", "position"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
    return 0


def _comment_author(raw: RawAPIDict) -> str:
    """Author login from a GitHub (``user.login``) or GitLab (``author.username``) comment."""
    for key, sub in (("user", "login"), ("author", "username")):
        container = raw.get(key)
        if isinstance(container, dict):
            value = cast("RawAPIDict", container).get(sub)
            if isinstance(value, str):
                return value
    return ""


class FindingsStore:
    """Durable per-PR JSON store of recorded findings + verdicts.

    One file per PR under the ``retro-findings`` data-dir namespace. Records
    accumulate across runs so the recurring-finding signal (the same
    fingerprint recorded against multiple PRs over time) is observable to the
    agent supplying the next run's classification.
    """

    def __init__(self, *, data_dir: Path | None = None) -> None:
        self._dir = data_dir if data_dir is not None else get_data_dir("retro-findings")

    def _path(self, pr_url: str) -> Path:
        slug = hashlib.sha256(pr_url.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{slug}.json"

    def load(self, pr_url: str) -> dict[str, str]:
        """Return the recorded ``fingerprint -> classification`` map for *pr_url*."""
        path = self._path(pr_url)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        recorded = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(recorded, dict):
            return {}
        return {str(k): str(v) for k, v in recorded.items()}

    def record(self, pr_url: str, classified: list[ClassifiedFinding]) -> None:
        """Persist the verdict for each finding, merging over prior records."""
        self._dir.mkdir(parents=True, exist_ok=True)
        merged = self.load(pr_url)
        for item in classified:
            merged[item.finding.fingerprint] = item.classification.value
        payload = {"pr_url": pr_url, "findings": merged}
        self._path(pr_url).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def recurring_fingerprints(self, *, min_occurrences: int = 2) -> set[str]:
        """Fingerprints recorded across at least *min_occurrences* PRs.

        A documented, reliable heuristic the agent may consult: a finding
        whose fingerprint has already been recorded on other PRs is, by
        definition, recurring — the strongest signal for class ``C``. The
        scaffold exposes the count; it never auto-applies the verdict.
        """
        counts: dict[str, int] = {}
        if not self._dir.is_dir():
            return set()
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            recorded = data.get("findings") if isinstance(data, dict) else None
            if not isinstance(recorded, dict):
                continue
            for fingerprint in recorded:
                counts[str(fingerprint)] = counts.get(str(fingerprint), 0) + 1
        return {fp for fp, count in counts.items() if count >= min_occurrences}


# Bare-reference detection primitives. A bare ``#1234`` / ``!99`` / Slack ts /
# forge URL interpolated verbatim into a filed enforcement issue would leak as
# an unclickable reference, so the untrusted finding text is neutralized before
# it is published. These regexes + ``find_bare_references`` detect every form a
# bare reference can take; a reference already wrapped in a markdown / angle
# link, inside a fenced/blockquote verbatim span, or in a leading close trailer
# is excised first so it is never flagged.
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]\([^)]*\)")
_ANGLE_LINK_RE: Final[re.Pattern[str]] = re.compile(r"<[^>\s]+(?:\|[^>]*)?>")
_FENCED_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_BLOCKQUOTE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*>.*$", re.MULTILINE)
_BARE_ISSUE_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])([#!]\d+)\b")
_BARE_SLACK_TS_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(\d{10}\.\d{6})(?![\w.])")
_BARE_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://(?:[\w.-]+\.)?(?:github\.com|gitlab\.com|notion\.so|notion\.site|slack\.com)/\S+",
)
_BODY_CLOSE_TRAILER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|relates?(?:\s*-\s*to|\s+to)?)(?:\s+part\s+of)?"
    r"(?::\s*|\s+)"
    r"(?:(?:[\w./-]+)?[#!]\d+|https?://\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_linked_spans(text: str) -> str:
    return _ANGLE_LINK_RE.sub(" ", _MARKDOWN_LINK_RE.sub(" ", text))


def _strip_verbatim_blocks(text: str) -> str:
    """Excise fenced code blocks and ``>`` blockquotes before bare-ref matching.

    Replaces each verbatim span with a space so a bare ref reproduced from
    external content (a quoted PR/MR description, a pasted comment) keeps the
    source's id form. Spacing keeps character offsets from merging adjacent
    tokens into a spurious new pattern.
    """
    return _BLOCKQUOTE_LINE_RE.sub(" ", _FENCED_BLOCK_RE.sub(" ", text))


def _strip_body_close_trailers(text: str) -> str:
    """Excise leading auto-close / relates trailer lines before bare-ref matching.

    Replaces each matching line with a space so character offsets stay stable
    and adjacent tokens cannot accidentally merge into a new pattern.
    """
    return _BODY_CLOSE_TRAILER_RE.sub(" ", text)


def find_bare_references(text: str) -> list[str]:
    if not text:
        return []
    unlinked = _strip_linked_spans(text)
    unlinked = _strip_verbatim_blocks(unlinked)
    unlinked = _strip_body_close_trailers(unlinked)
    refs: list[str] = []
    seen: set[str] = set()
    for pattern in (_BARE_ISSUE_RE, _BARE_SLACK_TS_RE):
        for match in pattern.finditer(unlinked):
            ref = match.group(0).rstrip(".,;:")
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def neutralize_bare_references(text: str) -> str:
    """Defang bare forge/Slack references so a published body has none.

    The review-comment body is untrusted: a bare ``#1234`` / ``!99`` / Slack
    ``ts`` / forge URL interpolated verbatim into the filed issue would leak
    as an unclickable bare reference. Each bare token is rewritten
    to a literal, non-auto-linking form — ``#N`` and ``!N`` become inline-code
    spans naming the kind (``issue N`` / ``MR N``) with the sigil dropped, a
    Slack ts becomes a code span with its dot replaced so the matcher no longer
    keys on it, and a forge/Notion/Slack URL becomes an angle autolink
    (clickable and excised by the scanner). The result passes
    :func:`find_bare_references` clean. URLs are wrapped first so a ``#`` in a
    URL fragment is protected inside the autolink before the ref pass runs.
    """
    text = _BARE_URL_RE.sub(lambda m: f"<{m.group(0)}>", text)
    text = _BARE_SLACK_TS_RE.sub(lambda m: f"`slack-ts {m.group(1).replace('.', '_')}`", text)

    def _ref(match: "re.Match[str]") -> str:
        token = match.group(1)
        kind = "issue" if token[0] == "#" else "MR"
        return f"`{kind} {token[1:]}`"

    return _BARE_ISSUE_RE.sub(_ref, text)


def build_issue_body(*, finding: ReviewFinding, enforcement: str, pr_url: str) -> str:
    """Render a scoped enforcement-issue body for one class-C finding.

    Clickable-link safe (the PR reference is a markdown link, never a bare
    ``#N``) and carries the hidden fingerprint marker the dedup search reads
    back. *enforcement* is the agent-supplied description of the smallest
    gate/test/hook that would prevent recurrence; *finding.body* is the
    untrusted review comment that surfaced the gap, so its bare references are
    neutralized (:func:`neutralize_bare_references`) before interpolation. The
    reviewer login is recorded as the abstract role ``reviewer`` so no
    individual is named. Banned-term scanning of the rendered body is the
    filer's responsibility (:func:`file_class_c_issue`), which withholds rather
    than leak.
    """
    file_anchor = f"`{finding.path}`" if finding.path else "(no file anchor)"
    safe_body = neutralize_bare_references(finding.body)
    return (
        "## Enforcement gap\n\n"
        f"A review finding recurred with no gate enforcing it. The smallest "
        f"enforcement artifact that would prevent recurrence:\n\n"
        f"{enforcement}\n\n"
        "## Source finding\n\n"
        f"- PR: [review thread]({pr_url})\n"
        f"- File: {file_anchor}\n\n"
        "> "
        f"{safe_body}\n\n"
        f"<!-- {_FINGERPRINT_MARKER} {finding.fingerprint} -->\n"
    )


def build_issue_title(finding: ReviewFinding) -> str:
    """A short, scoped issue title from the finding's first line.

    The snippet comes from the untrusted comment, so its bare references are
    neutralized (:func:`neutralize_bare_references`) the same as the body; the
    filer also banned-term-scans the title before filing.
    """
    first_line = _WHITESPACE_RE.sub(" ", finding.body).strip().split(". ")[0]
    snippet = neutralize_bare_references(first_line[:60].rstrip())
    return f"Enforcement gate for recurring review finding: {snippet}"


def find_existing_issue(host: "CodeHostBackend", *, repo: str, fingerprint: str) -> str:
    """Return the URL of an already-filed enforcement issue, or ``""``.

    Searches the repo's open issues for the fingerprint marker so a re-run of
    the command never refiles a class-C finding that already has a tracking
    issue (#1573 dedup). Best-effort against forge search indexing: an issue
    filed seconds ago may not yet be in the search index, so a back-to-back
    re-run could refile once — acceptable, and self-corrects on the next run.
    """
    matches = host.search_open_issues(repo=repo, query=_FINGERPRINT_MARKER + fingerprint)
    for raw in matches:
        body = str(raw.get("body") or raw.get("description") or "")
        if f"{_FINGERPRINT_MARKER} {fingerprint}" in body:
            return _issue_url(raw)
    return ""


def _issue_url(raw: RawAPIDict) -> str:
    """Pull the clickable issue URL from a created/searched issue payload."""
    for key in ("html_url", "web_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class FilingContext:
    """Per-run context for filing enforcement issues from a PR's findings.

    ``auto_filed`` is the souliane-account caveat: the factory files these
    issues *as* the maintainer's account, so the author-only auto-triage
    GitHub Action cannot tell them apart from a human-filed issue. Anything
    the agent files autonomously (the default) self-applies
    :data:`~teatree.core.models.implemented_issue_marker.NEEDS_TRIAGE_LABEL` so
    the loop's claim gate withholds it until the maintainer reviews. A caller
    that files at the user's direction sets ``auto_filed=False``.
    """

    repo: str
    pr_url: str
    label: str = "enforcement-gap"
    auto_filed: bool = True

    def labels(self) -> list[str]:
        return [self.label, NEEDS_TRIAGE_LABEL] if self.auto_filed else [self.label]


def file_class_c_issue(
    host: "CodeHostBackend",
    *,
    finding: ReviewFinding,
    enforcement: str,
    context: FilingContext,
) -> FiledIssue:
    """File (or find the already-filed) enforcement issue for a class-C finding.

    Dedup-first: if an open issue already carries this fingerprint marker, no
    new issue is filed and the existing URL is returned with
    ``already_filed=True``. Otherwise the title + body are rendered (with the
    untrusted finding text's bare references neutralized) and the rendered text
    is banned-term scanned: if it would leak a banned term (a customer/tenant
    name) the issue is **withheld** — never filed — so nothing leaks over the
    ``gh api`` stdin path. Otherwise a scoped issue is filed via the same
    backend the agent files issues through.
    """
    existing = find_existing_issue(host, repo=context.repo, fingerprint=finding.fingerprint)
    if existing:
        return FiledIssue(fingerprint=finding.fingerprint, url=existing, already_filed=True)

    title = build_issue_title(finding)
    body = build_issue_body(finding=finding, enforcement=enforcement, pr_url=context.pr_url)
    rendered = f"{title}\n{body}"

    banned = banned_terms_scanner.scan_text(rendered)
    if banned is not None:
        return FiledIssue(
            fingerprint=finding.fingerprint,
            url="",
            already_filed=False,
            withheld=True,
            withheld_reason=f"contains banned term '{banned}'",
        )

    # Defense in depth: neutralization should leave no bare ref, but never file
    # a body that would still leak an unclickable bare reference.
    leaked = find_bare_references(rendered)
    if leaked:
        return FiledIssue(
            fingerprint=finding.fingerprint,
            url="",
            already_filed=False,
            withheld=True,
            withheld_reason=f"contains bare reference(s): {', '.join(leaked)}",
        )

    raw = host.create_issue(repo=context.repo, title=title, body=body, labels=context.labels())
    return FiledIssue(fingerprint=finding.fingerprint, url=_issue_url(raw), already_filed=False)


def process_review_findings(
    host: "CodeHostBackend",
    *,
    classified: list[ClassifiedFinding],
    enforcement: dict[str, str],
    store: FindingsStore,
    context: FilingContext,
) -> ReviewFindingsSummary:
    """Record verdicts and file a deduped enforcement issue per class-C finding.

    Class A and B findings file nothing; class-C findings file one scoped,
    deduped issue each. *enforcement* maps a finding fingerprint to the
    agent-supplied description of the smallest gate; a class-C finding with no
    description falls back to a generic prompt to add one. Returns a summary
    with per-class counts and the filed-issue links.
    """
    store.record(context.pr_url, classified)
    counts = {cls.value: 0 for cls in FindingClass}
    filed: list[FiledIssue] = []
    for item in classified:
        counts[item.classification.value] += 1
        if item.classification is not FindingClass.C:
            continue
        description = enforcement.get(item.finding.fingerprint) or (
            "Add the smallest gate/test/hook that would deterministically prevent this finding from recurring."
        )
        filed.append(file_class_c_issue(host, finding=item.finding, enforcement=description, context=context))
    return ReviewFindingsSummary(pr_url=context.pr_url, counts=counts, filed=filed)
