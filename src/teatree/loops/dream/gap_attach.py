"""Fold pending dream gaps into an EXISTING host ticket — the one verb the backlog sweep and its fallback share.

The sweep picks the host exactly as it groups issues; this module only moves the gaps.
A terminal host is refused before a write; a host that turns terminal during the
forge write has that newly appended section removed before the gap is refused. A confirmed-private
host receives the full rule and citation; a public or unconfirmed host receives the key, scanned
short title and a pointer while the full substance stays in Ticket.context. Embedded peer headings
are demoted so each section's boundary stays unambiguous. The issue-write facade appends the section —
our tickets only, scrubbed, confirmed on the forge,
attributed to the sweep run — and every gap's fold, heading AND body, is re-read off the
issue before anything is recorded; otherwise the gaps stay pending.
Then the host carries the gaps in ``dream_gap_batch``, gains one rubric criterion that
blocks its merge until every gap is dispositioned, and an unplanned early host is routed
to planning with the gap list as the intent. A gap no host fits is attached to the
umbrella ticket itself, by the same verb; nothing here mints a ticket or retires a memory.
"""

import hashlib
import logging
import re
from dataclasses import dataclass

from django.db import transaction

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.gates.fold_preservation import fold_body, fold_marker
from teatree.core.gates.privacy_gate import scan_outbound_text
from teatree.core.issue_hygiene import (
    DescriptionRemoval,
    DescriptionSection,
    IssueWriteConflictError,
    append_description_section,
    remove_description_section,
    section_marker,
)
from teatree.core.issue_writes.section_removal import DescriptionRemovalConflictError
from teatree.core.models import ConsolidatedMemory, Rubric, Task, Ticket
from teatree.core.models.dream_gap_ledger import BATCH_KEY, pending_entries, take_pending
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_decision import has_plan_decision
from teatree.core.models.types import DreamGapEntry
from teatree.core.self_forge_identities import ExternalIssueRefusedError, require_self_authored_issue
from teatree.core.send_proxy import REDACTION_PLACEHOLDER, OutboundBlockedError, forge_from_url
from teatree.hooks.publish_destination import Destination, is_public_destination
from teatree.types import RawAPIDict
from teatree.utils.url_slug import project_slug_from_ref

DISPOSITION_CRITERION = (
    "Every folded dream gap carries a recorded disposition: `t3 dream gap-coverage --ticket {pk}` exits 0."
)
PLANNING_INTENT = (
    "Address or reject each dream gap folded into this ticket, recording each with "
    '`t3 dream gap-disposition {pk} <gap_key> --citation "<evidence>"` or '
    '`t3 dream gap-disposition {pk} <gap_key> --reject "<why>"`:\n{gaps}'
)
_SECTION_HEADING = "Dream gaps folded in"
logger = logging.getLogger(__name__)


class GapAttachError(ValueError):
    """A manifest naming a gap that is not pending on the umbrella."""


@dataclass(frozen=True, slots=True)
class AttachOutcome:
    host_pk: int
    attached: list[str]
    refusal: str = ""
    task: Task | None = None


@dataclass(frozen=True, slots=True)
class _FoldResult:
    refusal: str = ""
    marker: str = ""
    end_marker: str = ""
    original_body: str = ""
    section_body: str = ""


def attach_dream_gaps(
    host: Ticket,
    manifest: list[DreamGapEntry],
    *,
    umbrella: Ticket,
    code_host: CodeHostBackend,
    sweep_run_id: str = "",
) -> AttachOutcome:
    """Fold *manifest*'s pending gaps into *host*; record them only once the fold is proved on the forge."""
    host.refresh_from_db(fields=["state"])
    if host.is_settled:
        return AttachOutcome(
            host_pk=host.pk, attached=[], refusal=f"host {host.issue_url} is {host.state}: no new gaps"
        )
    themes = {str(item.get("gap_key") or ""): str(item.get("theme") or "") for item in manifest}
    umbrella.refresh_from_db(fields=["extra"])
    pending = {entry.get("gap_key"): entry for entry in pending_entries(umbrella)}
    unknown = sorted(key for key in themes if key not in pending)
    if unknown or not themes:
        msg = f"not pending on {umbrella.issue_url}: {', '.join(unknown) or '(empty manifest)'}"
        raise GapAttachError(msg)
    entries: list[DreamGapEntry] = [{**pending[key], "theme": theme} for key, theme in themes.items()]

    fold = _fold_into_host_issue(host, entries, code_host=code_host, sweep_run_id=sweep_run_id)
    if fold.refusal and not fold.marker:
        return AttachOutcome(host_pk=host.pk, attached=[], refusal=fold.refusal)

    terminal_state = ""
    attached: list[DreamGapEntry] = []
    task: Task | None = None
    with transaction.atomic():
        locked_host = Ticket.objects.select_for_update().get(pk=host.pk)
        if locked_host.is_settled:
            terminal_state = locked_host.state
        elif not fold.refusal:
            host.state = locked_host.state
            taken = {entry.get("gap_key") for entry in take_pending(umbrella, set(themes))}
            attached = [entry for entry in entries if entry.get("gap_key") in taken]
            host.merge_extra(append_to_lists={BATCH_KEY: list(attached)})
            context = locked_host.context
            for entry in attached:
                context = fold_body(
                    host_body=context,
                    member_ref=f"dream-gap {entry.get('gap_key')} (local context)",
                    member_title=str(entry.get("title") or ""),
                    member_body=_member_body(entry),
                )
            if context != locked_host.context:
                Ticket.objects.filter(pk=host.pk).update(context=context)
                host.context = context
            Rubric.add_criteria(host, [DISPOSITION_CRITERION.format(pk=host.pk)])
            task = _route(host, attached)
    if terminal_state:
        return _terminal_refusal(host, terminal_state, fold, code_host=code_host)
    if fold.refusal:
        return AttachOutcome(host_pk=host.pk, attached=[], refusal=fold.refusal)
    return AttachOutcome(host_pk=host.pk, attached=[str(entry.get("gap_key")) for entry in attached], task=task)


def _terminal_refusal(host: Ticket, state: str, fold: _FoldResult, *, code_host: CodeHostBackend) -> AttachOutcome:
    refusal = f"host {host.issue_url} is {state}: no new gaps"
    if fold.refusal:
        refusal += f"; {fold.refusal}"
    if fold.marker:
        try:
            _compensate_fold(host, fold, code_host=code_host)
        except Exception as exc:
            logger.exception("dream-gap fold compensation failed for terminal host %s", host.issue_url)
            refusal += f"; compensation failed ({exc}): fold may remain on a terminal host"
    return AttachOutcome(host_pk=host.pk, attached=[], refusal=refusal)


def _member_body(entry: DreamGapEntry) -> str:
    row = ConsolidatedMemory.objects.filter(cluster_key=entry.get("cluster_key") or entry.get("gap_key")).first()
    parts = [f"Dream gap `{entry.get('gap_key')}` — citation: {entry.get('citation') or 'unknown'}."]
    if entry.get("detail"):
        parts.append(str(entry["detail"]).strip())
    if row is not None:
        parts.append(row.rule.strip())
        if row.verified_citation.strip():
            parts.append(f"Cited: {row.verified_citation.strip()}")
    return "\n\n".join(parts)


def _members_for_host_issue(host: Ticket, entries: list[DreamGapEntry]) -> list[tuple[str, str, str]]:
    forge = forge_from_url(host.issue_url)
    repo = project_slug_from_ref(host.issue_url) or host.issue_url
    try:
        public = is_public_destination(Destination(slug=host.issue_url, via="url", forge=forge))
    except Exception:
        logger.warning("dream-gap visibility unresolved for %s; treating as public", host.issue_url, exc_info=True)
        public = True
    members = []
    for entry in entries:
        title = str(entry.get("title") or "")
        if public and scan_outbound_text(text=title, target_repo=repo, forge=forge).refused:
            msg = f"short title for dream-gap {entry.get('gap_key')} failed the publication scan"
            raise OutboundBlockedError(msg)
        body = "Full rule and citation are in the local ticket context." if public else _member_body(entry)
        members.append((f"dream-gap {entry.get('gap_key')}", title, body))
    return members


def _fold_into_host_issue(
    host: Ticket, entries: list[DreamGapEntry], *, code_host: CodeHostBackend, sweep_run_id: str
) -> _FoldResult:
    """Append and prove the gaps, returning the newly written section for compensation."""
    try:
        members = _members_for_host_issue(host, entries)
        current = _description(require_self_authored_issue(host=code_host, issue_url=host.issue_url))
        all_key = _section_key(members)
        all_body = _render_members(members)
        existing = _fold_result(all_key, all_body, original_body=current)
        if found := _existing_fold(current, existing):
            if found.marker != existing.marker or not all(
                _carries_fold(current, ref, body) for ref, _title, body in members
            ):
                current = _repair_fold(host, current, found, code_host=code_host, replacement_marker=existing.marker)
            marker = ""
            end_marker = ""
            section_body = ""
        else:
            missing = [member for member in members if not _carries_fold(current, member[0], member[2])]
            if missing:
                section_body = _render_members(missing)
                fold = _fold_result(_section_key(missing), section_body, original_body=current)
                if found := _existing_fold(current, fold):
                    current = _repair_fold(host, current, found, code_host=code_host, replacement_marker=fold.marker)
                    marker = ""
                else:
                    outcome = append_description_section(
                        host=code_host,
                        issue_url=host.issue_url,
                        section=DescriptionSection(
                            heading=_SECTION_HEADING,
                            body=f"{section_body}\n\n{fold.end_marker}",
                            action="dream_gap_attach",
                            identity=_section_key(missing),
                        ),
                        sweep_run_id=sweep_run_id,
                    )
                    if outcome.kind == "already_present":
                        landed = _description(require_self_authored_issue(host=code_host, issue_url=host.issue_url))
                        current = _repair_fold(host, landed, fold, code_host=code_host)
                        marker = ""
                    else:
                        marker = fold.marker
                end_marker = fold.end_marker
            else:
                marker = ""
                end_marker = ""
                section_body = ""
        landed = _description(require_self_authored_issue(host=code_host, issue_url=host.issue_url))
    except (
        DescriptionRemovalConflictError,
        ExternalIssueRefusedError,
        IssueWriteConflictError,
        OutboundBlockedError,
    ) as exc:
        return _FoldResult(refusal=f"the fold into {host.issue_url} did not land: {exc}")
    missing = [ref for ref, _title, member_body in members if not _carries_fold(landed, ref, member_body)]
    if missing:
        return _FoldResult(
            refusal=f"the host issue {host.issue_url} lost the fold of: {', '.join(missing)}",
            marker=marker,
            end_marker=end_marker,
            original_body=current,
            section_body=section_body,
        )
    return _FoldResult(marker=marker, end_marker=end_marker, original_body=current, section_body=section_body)


def _section_key(members: list[tuple[str, str, str]]) -> str:
    refs = sorted(fold_marker(ref) for ref, _title, _body in members)
    return hashlib.sha256("\x1f".join(refs).encode()).hexdigest()[:16]


def _render_members(members: list[tuple[str, str, str]]) -> str:
    section = ""
    for ref, title, body in sorted(members, key=lambda member: fold_marker(member[0])):
        section = fold_body(host_body=section, member_ref=ref, member_title=title, member_body=_demote_headings(body))
    return section.rstrip()


def _fold_result(key: str, body: str, *, original_body: str) -> _FoldResult:
    return _FoldResult(
        marker=section_marker(key),
        end_marker=f"<!-- t3-dream-gap-fold-end:{key} -->",
        original_body=original_body,
        section_body=body,
    )


def _existing_fold(description: str, fold: _FoldResult) -> _FoldResult | None:
    return fold if fold.marker in description else None


def _repair_fold(
    host: Ticket, current: str, fold: _FoldResult, *, code_host: CodeHostBackend, replacement_marker: str | None = None
) -> str:
    start, end, header, complete = _validated_fold(current, fold)
    if complete:
        return current
    generated = (replacement_marker or fold.marker) + header + fold.section_body + "\n\n" + fold.end_marker
    replacement = current[:start] + generated + current[end:]
    remove_description_section(
        host=code_host,
        issue_url=host.issue_url,
        removal=DescriptionRemoval(
            marker=fold.marker,
            expected_body=current,
            replacement_body=replacement,
            action="dream_gap_repair",
            retain_marker=fold.marker == (replacement_marker or fold.marker),
            generated_span=(start, start + len(generated)),
        ),
    )
    return replacement


def _compensate_fold(host: Ticket, fold: _FoldResult, *, code_host: CodeHostBackend) -> None:
    current = _description(require_self_authored_issue(host=code_host, issue_url=host.issue_url))
    proposed = _without_appended_fold(current, fold)
    if proposed == current:
        return
    remove_description_section(
        host=code_host,
        issue_url=host.issue_url,
        removal=DescriptionRemoval(
            marker=fold.marker,
            expected_body=current,
            replacement_body=proposed,
            action="dream_gap_compensate",
        ),
    )


def _without_appended_fold(description: str, fold: _FoldResult) -> str:
    start, end, _header, _complete = _validated_fold(description, fold)
    prefix = description[:start]
    if prefix == f"{fold.original_body.rstrip()}\n\n":
        prefix = fold.original_body
    elif prefix.endswith("\n\n"):
        prefix = prefix[:-2]
    tail = description[end:].removeprefix("\n\n")
    if tail and prefix:
        prefix += "\n" if prefix.endswith("\n") else "\n\n"
    return prefix + tail


def _validated_fold(description: str, fold: _FoldResult) -> tuple[int, int, str, bool]:
    starts = list(re.finditer(r"(?m)^<!-- t3-hygiene:[0-9a-f]{16} -->$", description))
    matching = [match for match in starts if match.group() == fold.marker]
    if len(matching) != 1:
        msg = f"fold section {fold.marker} has no unique start marker"
        raise IssueWriteConflictError(msg)
    start = matching[0]
    ends = list(re.finditer(rf"(?m)^{re.escape(fold.end_marker)}$", description[start.end() :]))
    if len(ends) != 1:
        msg = f"fold section {fold.marker} has no unique matching end marker"
        raise IssueWriteConflictError(msg)
    end = ends[0]
    end_start = start.end() + end.start()
    if any(start.end() < other.start() < end_start for other in starts):
        msg = f"fold section {fold.marker} contains a nested start marker"
        raise IssueWriteConflictError(msg)
    inner = description[start.end() : end_start]
    header_match = re.match(r"\n\n## Dream gaps folded in \(\d{4}-\d{2}-\d{2}\)\n\n", inner)
    if header_match is None or not inner.endswith("\n\n"):
        msg = f"fold section {fold.marker} does not match the rendered heading"
        raise IssueWriteConflictError(msg)
    body = inner[header_match.end() : -2]
    if not fold.section_body.startswith(body):
        msg = f"fold section {fold.marker} differs from the rendered body"
        raise IssueWriteConflictError(msg)
    return start.start(), start.end() + end.end(), inner[: header_match.end()], body == fold.section_body


def _carries_fold(description: str, ref: str, member_body: str) -> bool:
    """True when a fold headed exactly *ref* carries every line of *member_body*, scrub redactions allowed."""
    heading = re.compile(rf"^{re.escape(fold_marker(ref))}(?: — .*)?$", re.MULTILINE)
    wanted = _lines(_demote_headings(member_body))
    for match in heading.finditer(description):
        block = [_redaction_tolerant(kept) for kept in _lines(_up_to_next_heading(description[match.end() :]))]
        if all(any(kept is not None and kept.fullmatch(line) for kept in block) for line in wanted):
            return True
    return False


def _up_to_next_heading(text: str) -> str:
    boundary = re.search(r"(?m)^ {0,3}#{1,2}(?:[ \t]+|$)|^[^\n]*\S[^\n]*\n {0,3}(?:=+|-+)[ \t]*(?=\n|$)", text)
    return text[: boundary.start()] if boundary else text


def _demote_headings(text: str) -> str:
    text = re.sub(r"(?m)^([^\n]*\S[^\n]*)\n {0,3}(?:=+|-+)[ \t]*(?=\n|$)", r"### \1", text)
    return re.sub(r"(?m)^( {0,3})#{1,2}(?=[ \t]|$)", r"\1###", text)


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _redaction_tolerant(kept: str) -> re.Pattern[str] | None:
    parts = kept.split(REDACTION_PLACEHOLDER)
    if not any(part.strip() for part in parts):
        return None
    return re.compile(".+?".join(re.escape(part) for part in parts))


def _description(issue: RawAPIDict) -> str:
    return str(issue.get("description") or issue.get("body") or "")


def _route(host: Ticket, attached: list[DreamGapEntry]) -> Task | None:
    """Route an unplanned author host to planning around the gaps; a planned host keeps its own work.

    An unplanned host past the early states has no planning rung left; the rubric
    criterion still blocks its merge until every gap is dispositioned.
    """
    if not attached or has_plan_decision(host) or host.role != Ticket.Role.AUTHOR:
        return None
    gaps = "\n".join(f"- [{entry.get('gap_key')}] {entry.get('title', '')}" for entry in attached)
    try:
        return host.schedule_implementing("coding", reason=PLANNING_INTENT.format(pk=host.pk, gaps=gaps))
    except NoPlanArtifactError:
        return None
