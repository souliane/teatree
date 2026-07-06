"""Build, diff, and gate a ticket's attachment manifest (PR-15, M5).

Ticket intake must fetch every spec attachment a ticket references *before* the
planner runs, or the planner designs against prose that silently omits the
attached PDF / mockup / screenshot. This module is the deterministic engine
behind :class:`teatree.core.models.AttachmentManifest`.

``extract_refs`` is pure regex extraction of every attachment URL a ticket's
issue body + comments reference, classified into ``gitlab-upload`` / ``notion`` /
``slack`` (no transport — the caller supplies the text). ``build_manifest``
reconciles the extracted refs against what is already cached on disk under
``<ticket_dir>/.attachments/`` (ground truth: the file exists) and persists an
idempotent snapshot (no new row when unchanged). ``attachment_gate_refusal`` is
the intake gate — ``None`` when every referenced attachment is fetched (vacuous
on a zero-attachment ticket), else a refusal naming the missing URLs plus the
exact ``--fetch`` command. ``fetch_manifest`` downloads the un-fetched entries
through an injected fetch seam (default: ``default_fetcher``), then re-records.

The transport lives behind two injected seams — ``code_host`` for reading the
issue text and ``fetcher`` for downloading — so the diff/gate logic is pure and
exhaustively testable, and this module never imports a concrete backend.
"""

import hashlib
import logging
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.intake.attachment_fetch_registry import resolve_attachment_fetcher
from teatree.core.models import AttachmentManifest
from teatree.core.worktree.worktree_paths import ticket_dir_for

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


class AttachmentKind(StrEnum):
    """The referenced-attachment sources the manifest recognises and fetches."""

    GITLAB_UPLOAD = "gitlab-upload"
    NOTION = "notion"
    SLACK = "slack"


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """One attachment URL referenced by a ticket, classified by source."""

    source_url: str
    kind: AttachmentKind


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One manifest row: a referenced attachment + its cached-file state.

    ``fetched`` is ground truth — an entry counts as fetched only when its
    recorded ``local_path`` still exists on disk, so a manifest whose cache was
    wiped re-reports the entry as missing rather than trusting a stale flag.
    """

    source_url: str
    kind: str
    local_path: str = ""
    fetched_at: str = ""

    @property
    def fetched(self) -> bool:
        return bool(self.local_path) and Path(self.local_path).exists()

    def to_dict(self) -> dict[str, str]:
        return {
            "source_url": self.source_url,
            "kind": self.kind,
            "local_path": self.local_path,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "ManifestEntry":
        return cls(
            source_url=str(data.get("source_url", "")),
            kind=str(data.get("kind", "")),
            local_path=str(data.get("local_path", "")),
            fetched_at=str(data.get("fetched_at", "")),
        )


# A GitLab upload reference is ``/uploads/<32-hex>/<filename>`` — relative
# (``![f](/uploads/…)``) or absolute (``https://host/group/proj/uploads/…``).
_GITLAB_UPLOAD = re.compile(r"(?:https?://[^\s()\[\]\"'<>]+)?/uploads/[0-9a-f]{32}/[^\s()\[\]\"'<>]+")
_NOTION = re.compile(r"https?://[^\s()\[\]\"'<>]*notion\.(?:so|site)/[^\s()\[\]\"'<>]+")
# Slack thread/message permalinks (``…slack.com/archives/…``) and file links
# (``files.slack.com/…``) — the two shapes an attachment can take in a thread.
_SLACK = re.compile(
    r"https?://(?:files\.slack\.com/[^\s()\[\]\"'<>]+|[^\s()\[\]\"'<>]*\.slack\.com/archives/[^\s()\[\]\"'<>]+)"
)

_PATTERNS: list[tuple[AttachmentKind, re.Pattern[str]]] = [
    (AttachmentKind.GITLAB_UPLOAD, _GITLAB_UPLOAD),
    (AttachmentKind.NOTION, _NOTION),
    (AttachmentKind.SLACK, _SLACK),
]


def _refs_in_text(text: str) -> list[AttachmentRef]:
    """Every attachment ref in one text, in order of first appearance."""
    found: list[tuple[int, AttachmentRef]] = [
        (match.start(), AttachmentRef(match.group(0), kind))
        for kind, pattern in _PATTERNS
        for match in pattern.finditer(text)
    ]
    return [ref for _, ref in sorted(found, key=operator.itemgetter(0))]


def extract_refs(texts: list[str]) -> list[AttachmentRef]:
    """Extract every referenced attachment URL across *texts*, de-duplicated.

    Deterministic and transport-free: the caller supplies the ticket's issue body
    + comment bodies. Order is stable (first-seen); a URL referenced twice yields
    one ref.
    """
    seen: set[str] = set()
    refs: list[AttachmentRef] = []
    for text in texts:
        for ref in _refs_in_text(text):
            if ref.source_url not in seen:
                seen.add(ref.source_url)
                refs.append(ref)
    return refs


def local_path_for(attachments_dir: Path, ref: AttachmentRef) -> Path:
    """Deterministic cache path for *ref* under *attachments_dir*.

    ``<12-hex-of-url>-<basename>`` so two URLs never collide and the fetch step +
    the build step agree on where the file lives. The URL hash prefix keeps the
    name stable even when two attachments share a basename.
    """
    digest = hashlib.sha1(ref.source_url.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — cache-key only, not a security digest
    basename = Path(ref.source_url.split("?", 1)[0]).name or "attachment"
    return attachments_dir / f"{digest}-{basename}"


def attachments_dir_for(ticket: "Ticket", *, workspace: Path) -> Path:
    """The ``<ticket_dir>/.attachments/`` cache dir for *ticket*.

    Keyed on the ticket's provisioned branch (``extra['branch']``) so the cache
    sits beside the worktrees; falls back to a stable ``ticket-<pk>`` dir for a
    ticket that has not been scoped to a branch yet.
    """
    branch = str((ticket.extra or {}).get("branch") or f"ticket-{ticket.pk}")
    return ticket_dir_for(workspace, branch) / ".attachments"


def _entries_of(manifest: "AttachmentManifest | None") -> list[ManifestEntry]:
    if manifest is None:
        return []
    return [ManifestEntry.from_dict(entry) for entry in (manifest.entries or [])]


def build_manifest(
    ticket: "Ticket",
    *,
    texts: list[str],
    attachments_dir: Path,
    recorded_by: str = "t3:intake",
) -> AttachmentManifest:
    """Reconcile referenced attachments against the on-disk cache and persist.

    Extracts refs from *texts*, marks each fetched iff its cached file exists
    (ground truth — preserving a prior entry's ``fetched_at`` when the file is
    still there), and records the snapshot. Idempotent: when the reconciled
    entries equal the latest snapshot's, no new row is written and the latest is
    returned, so a re-survey with an unchanged set never grows the audit trail.
    """
    refs = extract_refs(texts)
    latest = AttachmentManifest.latest_for(ticket)
    prior = {entry.source_url: entry for entry in _entries_of(latest)}

    entries: list[ManifestEntry] = []
    for ref in refs:
        previous = prior.get(ref.source_url)
        if previous is not None and previous.fetched:
            entries.append(previous)
            continue
        cached = local_path_for(attachments_dir, ref)
        if cached.exists():
            fetched_at = timezone.now().isoformat(timespec="seconds")
            entries.append(ManifestEntry(ref.source_url, ref.kind, str(cached), fetched_at))
        else:
            entries.append(ManifestEntry(ref.source_url, ref.kind))

    entry_dicts = [entry.to_dict() for entry in entries]
    if latest is not None and (latest.entries or []) == entry_dicts:
        return latest
    return AttachmentManifest.record(ticket=ticket, entries=entry_dicts, recorded_by=recorded_by)


def unfetched_entries(manifest: AttachmentManifest) -> list[ManifestEntry]:
    """The manifest entries whose cached file is absent (ground truth)."""
    return [entry for entry in _entries_of(manifest) if not entry.fetched]


def attachment_gate_refusal(
    ticket: "Ticket",
    *,
    texts: list[str],
    attachments_dir: Path,
    fetch_command: str,
) -> str | None:
    """Intake gate verdict: ``None`` to hand off, else a refusal message.

    Builds the manifest and returns ``None`` when every referenced attachment is
    fetched — vacuously so for a zero-attachment ticket. Otherwise returns a
    refusal listing each missing URL and the exact ``--fetch`` command that
    resolves it, so the planner is never handed a ticket whose spec attachments
    are still un-downloaded.
    """
    manifest = build_manifest(ticket, texts=texts, attachments_dir=attachments_dir)
    missing = unfetched_entries(manifest)
    if not missing:
        return None
    listed = "\n".join(f"  - {entry.source_url} ({entry.kind})" for entry in missing)
    return (
        f"{len(missing)} attachment(s) not fetched for ticket {ticket.pk}:\n{listed}\nFetch them with: {fetch_command}"
    )


def ticket_text_sources(ticket: "Ticket", *, code_host: "CodeHostBackend | None") -> list[str]:
    """The issue body + every comment body for *ticket*, via the code-host seam.

    Fail-open: no backend, an unparsable issue URL, or any transport error
    degrades to an empty list (a forge outage must never wedge the FSM — the gate
    then reads "no attachments" and hands off, mirroring the landscape survey's
    best-effort doctrine). ``get_issue`` bodies live under ``body`` (GitHub) or
    ``description`` (GitLab); notes/comments under ``body`` on both.
    """
    if code_host is None or not ticket.issue_url:
        return []
    texts: list[str] = []
    try:
        issue = code_host.get_issue(ticket.issue_url)
        for key in ("body", "description"):
            value = issue.get(key)
            if isinstance(value, str) and value:
                texts.append(value)
        for comment in code_host.list_issue_comments(issue_url=ticket.issue_url):
            body = comment.get("body")
            if isinstance(body, str) and body:
                texts.append(body)
    except Exception:
        logger.warning("attachment manifest: reading issue text failed for ticket %s", ticket.pk, exc_info=True)
        return []
    return texts


class AttachmentFetchError(Exception):
    """A single attachment could not be downloaded through the fetch seam."""


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Per-entry result of a ``--fetch`` pass — ``ok`` plus a human detail."""

    source_url: str
    kind: str
    ok: bool
    detail: str


Fetcher = Callable[[AttachmentRef, Path], Path]
"""Download seam: fetch *ref* to the given path, return it, raise on failure."""


def default_fetcher(ref: AttachmentRef, dest: Path) -> Path:
    """Download *ref* to *dest* via the fetcher ``teatree.backends`` registered.

    Resolves the per-kind transport from
    :mod:`teatree.core.intake.attachment_fetch_registry` (which ``backends`` populates at
    app-ready time — core never imports backends). A source with no registered
    transport raises :class:`AttachmentFetchError` naming the exact ``dest`` path
    to place the file at — the gate checks that deterministic hashed path
    (``local_path_for``), so a file dropped under its natural basename would not
    clear the hold; the full path is what makes manual placement work.
    """
    fetcher = resolve_attachment_fetcher(ref.kind)
    if fetcher is None:
        msg = f"no fetch transport registered for kind={ref.kind}; download {ref.source_url} to {dest} then re-run"
        raise AttachmentFetchError(msg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return fetcher(ref, dest)


def fetch_manifest(
    ticket: "Ticket",
    *,
    texts: list[str],
    attachments_dir: Path,
    fetcher: Fetcher = default_fetcher,
    recorded_by: str = "t3:attachments",
) -> tuple[AttachmentManifest, list[FetchOutcome]]:
    """Download every un-fetched entry through *fetcher*, then re-record.

    Returns the refreshed manifest (rebuilt from disk after the downloads land)
    and one :class:`FetchOutcome` per attempted entry. A fetch failure is
    recorded as ``ok=False`` and left un-fetched — the gate keeps holding the
    ticket, never a silent pass.
    """
    manifest = build_manifest(ticket, texts=texts, attachments_dir=attachments_dir)
    outcomes: list[FetchOutcome] = []
    for entry in unfetched_entries(manifest):
        ref = AttachmentRef(entry.source_url, AttachmentKind(entry.kind))
        dest = local_path_for(attachments_dir, ref)
        try:
            fetcher(ref, dest)
            outcomes.append(FetchOutcome(ref.source_url, ref.kind, ok=True, detail=str(dest)))
        except (AttachmentFetchError, OSError, ValueError) as exc:
            outcomes.append(FetchOutcome(ref.source_url, ref.kind, ok=False, detail=str(exc)))
    refreshed = build_manifest(ticket, texts=texts, attachments_dir=attachments_dir, recorded_by=recorded_by)
    return refreshed, outcomes
