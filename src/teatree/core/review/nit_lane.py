"""Publish advisory review points inline and track them in one follow-up issue."""

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import PartialReviewPublishError, PrReview, PrReviewComment
from teatree.core.gates.privacy_gate import format_refusal, scan_outbound_text
from teatree.core.issue_hygiene import IssueDraft, create_or_extend_by_marker, open_candidates
from teatree.core.modelkit.task_failure_taxonomy import COMMENT_CHECK_PREFIX, NIT_HEAD_MOVED
from teatree.core.on_behalf_gate_recorded import OnBehalfPartialPublishError, require_on_behalf_approval
from teatree.core.review.comment_checks import added_lines, comment_refusal, review_refusal
from teatree.core.review.review_candidate import is_self_authored
from teatree.core.self_forge_identities import confirmed_self_identity_set, require_self_authored_issue
from teatree.core.send_proxy import OutboundBlockedError, route_forge_write
from teatree.on_behalf_gate import OnBehalfContext
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.types import RawAPIDict
    from teatree.utils.pr_ref import PrRef


@dataclass(frozen=True, slots=True)
class NitPoint:
    file: str
    line: int
    summary: str
    permalink: str = ""


@dataclass(frozen=True, slots=True)
class NitReceipt:
    issue_url: str
    thread_ids: tuple[str, ...]
    head_sha: str


@dataclass(frozen=True, slots=True)
class _NitContext:
    ref: "PrRef"
    pr_url: str
    head_sha: str
    own_pr: bool


class NitLaneError(RuntimeError):
    pass


def validate_nit_comments(points: tuple[NitPoint, ...], *, pr_url: str = "") -> None:
    ref = pr_ref_from_url(pr_url) if pr_url else None
    for point in points:
        review = PrReview(
            commit_sha="",
            body="",
            comments=(PrReviewComment(path=point.file, line=point.line, body=f"Nit: {point.summary}"),),
            marker="",
        )
        if refusal := review_refusal(review, file_diffs=dict):
            message = f"{COMMENT_CHECK_PREFIX}{refusal}; revise the NIT and retry this head"
            raise NitLaneError(message)
        if ref is not None:
            scan = scan_outbound_text(
                text=f"{point.file}:{point.line}\nNit: {point.summary}", target_repo=ref.slug, forge=ref.host_kind
            )
            if scan.refused:
                message = f"comment check — {format_refusal(scan)}; revise the NIT and retry this head"
                raise NitLaneError(message)


def nit_pr_marker(pr_url: str) -> str:
    digest = hashlib.sha256(pr_url.encode()).hexdigest()[:16]
    return f"<!-- t3-nit-pr:{digest} -->"


def render_follow_up_body(*, pr_url: str, marker: str, points: tuple[NitPoint, ...]) -> str:
    lines = [f"Non-blocking points from {pr_url} {marker}"]
    lines.extend(
        f"{index}. [{point.file}:{point.line}]({point.permalink}) — {point.summary}"
        for index, point in enumerate(points, 1)
    )
    lines.append("These points do not block the merge and are tracked in this issue.")
    return "\n".join(lines)


def nit_point_marker(pr_url: str, head_sha: str, point: NitPoint) -> str:
    identity = "\x1f".join((pr_url, head_sha, point.file, str(point.line), point.summary))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"<!-- t3-nit-point:{digest} -->"


def _thread_for_marker(threads: list["RawAPIDict"], marker: str, pr_url: str) -> tuple[str, str, bool] | None:
    for thread in threads:
        notes = thread.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict) or marker not in str(note.get("body") or ""):
                continue
            thread_id = thread.get("id")
            note_id = note.get("id")
            permalink = note.get("url") or note.get("web_url")
            if not permalink and isinstance(note_id, int):
                permalink = f"{pr_url}#note_{note_id}"
            if not isinstance(thread_id, str) or not thread_id or not isinstance(permalink, str) or not permalink:
                message = "Inline NIT has no thread id or permalink"
                raise NitLaneError(message)
            return thread_id, permalink, _resolved(thread)
    return None


def _resolved(thread: "RawAPIDict") -> bool:
    return thread.get("isResolved") is True or thread.get("resolved") is True


def _post_missing(
    host: "CodeHostBackend", *, pr_url: str, head_sha: str, points: tuple[NitPoint, ...], own_pr: bool
) -> None:
    ref = pr_ref_from_url(pr_url)
    if ref is None:
        message = "NIT pull request URL is unrecognized"
        raise NitLaneError(message)
    comments = tuple(
        PrReviewComment(
            path=point.file,
            line=point.line,
            body=route_forge_write(
                forge=ref.host_kind,
                repo=ref.slug,
                text=f"Nit: {point.summary}\n{nit_point_marker(pr_url, head_sha, point)}",
                action="post_review_nit",
                target=f"{ref.slug}#{ref.pr_id}",
            ),
        )
        for point in points
    )
    review = PrReview(commit_sha=head_sha, body="", comments=comments, marker="")
    if refusal := review_refusal(review, file_diffs=lambda: host.get_pr_file_diffs(repo=ref.slug, pr_iid=ref.pr_id)):
        message = f"{COMMENT_CHECK_PREFIX}{refusal}"
        raise NitLaneError(message)

    def publish() -> "RawAPIDict":
        try:
            result = host.submit_pr_review(repo=ref.slug, pr_iid=ref.pr_id, review=review)
        except PartialReviewPublishError as exc:
            raise OnBehalfPartialPublishError(("Inline NIT review partly posted", exc.landed)) from exc
        if result.get("error"):
            message = f"Inline NIT review refused: {result['error']}"
            raise NitLaneError(message)
        return result

    require_on_behalf_approval(
        target=f"{ref.slug}#{ref.pr_id}",
        action="post_review_nit",
        publish=publish,
        context=OnBehalfContext(own_mr=own_pr, target=ref.slug),
    )


def _target(host: "CodeHostBackend", pr_url: str, head_sha: str, points: tuple[NitPoint, ...]) -> _NitContext:
    ref = pr_ref_from_url(pr_url)
    if ref is None:
        message = "NIT pull request URL is unrecognized"
        raise NitLaneError(message)
    if not points or any(not point.summary.strip() for point in points):
        message = "Every NIT needs a summary"
        raise NitLaneError(message)
    if host.fetch_live_head_sha(slug=ref.slug, pr_id=ref.pr_id) != head_sha:
        message = NIT_HEAD_MOVED
        raise NitLaneError(message)
    identities = confirmed_self_identity_set(pr_url, host=host)
    own_pr = is_self_authored(pr_url, host, identities) if identities is not None else None
    if own_pr is None:
        message = "NIT review authorship could not be confirmed"
        raise NitLaneError(message)
    return _NitContext(ref, pr_url, head_sha, own_pr)


def _paired_comments(
    host: "CodeHostBackend", context: _NitContext, points: tuple[NitPoint, ...]
) -> tuple[tuple[NitPoint, ...], tuple[str, ...]]:
    ref, pr_url, head_sha = context.ref, context.pr_url, context.head_sha
    diffs = host.get_pr_file_diffs(repo=ref.slug, pr_iid=ref.pr_id)
    inline = tuple(
        point
        for point in points
        if point.file
        and point.line > 0
        and isinstance(diffs.get(point.file), str)
        and point.line in added_lines(diffs[point.file] or "")
    )
    threads = host.list_review_threads(repo=ref.slug, pr_iid=ref.pr_id)
    missing = tuple(
        point
        for point in inline
        if _thread_for_marker(threads, nit_point_marker(pr_url, head_sha, point), pr_url) is None
    )
    if missing:
        _post_missing(host, pr_url=pr_url, head_sha=head_sha, points=missing, own_pr=context.own_pr)
        threads = host.list_review_threads(repo=ref.slug, pr_iid=ref.pr_id)
    paired: list[NitPoint] = []
    thread_ids: list[str] = []
    for point in points:
        if point not in inline:
            paired.append(NitPoint(point.file or "PR", point.line, point.summary, pr_url))
            thread_ids.append("issue-only")
            continue
        found = _thread_for_marker(threads, nit_point_marker(pr_url, head_sha, point), pr_url)
        if found is None:
            message = "Inline NIT comment did not read back"
            raise NitLaneError(message)
        thread_id, permalink, _ = found
        paired.append(NitPoint(point.file, point.line, point.summary, permalink))
        thread_ids.append(thread_id)
    return tuple(paired), tuple(thread_ids)


def _follow_up_issue(
    host: "CodeHostBackend",
    context: _NitContext,
    *,
    paired: tuple[NitPoint, ...],
    thread_ids: tuple[str, ...],
) -> str:
    ref, pr_url = context.ref, context.pr_url
    marker = nit_pr_marker(pr_url)
    body = render_follow_up_body(pr_url=pr_url, marker=marker, points=paired)
    matching = [
        url
        for url, issue in open_candidates(host=host, repo=ref.slug).items()
        if marker in str(issue.get("body") or issue.get("description") or "")
    ]
    if len(matching) > 1:
        message = "Multiple NIT follow-up issues carry this pull request marker"
        raise NitLaneError(message)
    if matching:
        require_self_authored_issue(host=host, issue_url=matching[0])
        issue_url = matching[0]
    else:
        issue_url = _create_follow_up_issue(host, ref, thread_ids=thread_ids, body=body, marker=marker)
    issue = _ensure_follow_up_body(host, context, issue_url, paired, host.get_issue(issue_url))
    _ensure_follow_up_labels(host, context, issue_url, issue)
    return issue_url


def _ensure_follow_up_body(
    host: "CodeHostBackend", context: _NitContext, issue_url: str, paired: tuple[NitPoint, ...], issue: "RawAPIDict"
) -> "RawAPIDict":
    ref, pr_url, own_pr = context.ref, context.pr_url, context.own_pr
    marker = nit_pr_marker(pr_url)
    live_body = issue.get("body") or issue.get("description")
    if not isinstance(live_body, str) or marker not in live_body:
        message = "NIT follow-up issue did not read back with its inline permalinks"
        raise NitLaneError(message)
    complete_body = _canonical_follow_up_body(pr_url=pr_url, marker=marker, existing=live_body, new=paired)
    if live_body.strip() != complete_body:
        require_self_authored_issue(host=host, issue_url=issue_url)
        scrubbed = route_forge_write(
            forge=ref.host_kind,
            repo=ref.slug,
            text=complete_body,
            action="nit_follow_up_update",
            target=issue_url,
        )

        def update_body() -> "RawAPIDict":
            result = host.update_issue(issue_url=issue_url, body=scrubbed)
            if result.get("error"):
                message = f"NIT follow-up update was refused: {result['error']}"
                raise NitLaneError(message)
            return result

        require_on_behalf_approval(
            target=issue_url,
            action="nit_follow_up_update",
            publish=update_body,
            context=OnBehalfContext(own_mr=own_pr, target=ref.slug),
        )
        issue = host.get_issue(issue_url)
        if (issue.get("body") or issue.get("description")) != scrubbed:
            message = "NIT follow-up issue update did not read back"
            raise NitLaneError(message)
    return issue


def _ensure_follow_up_labels(
    host: "CodeHostBackend", context: _NitContext, issue_url: str, issue: "RawAPIDict"
) -> None:
    ref, own_pr = context.ref, context.own_pr
    missing_labels = {"nit", "t3-auto"} - _issue_labels(issue)
    if missing_labels:
        require_self_authored_issue(host=host, issue_url=issue_url)

        def add_labels() -> "RawAPIDict":
            result = host.add_issue_labels(issue_url=issue_url, labels=tuple(sorted(missing_labels)))
            if result.get("error"):
                message = f"NIT follow-up labels were refused: {result['error']}"
                raise NitLaneError(message)
            return result

        require_on_behalf_approval(
            target=issue_url,
            action="nit_follow_up_labels",
            publish=add_labels,
            context=OnBehalfContext(own_mr=own_pr, target=ref.slug),
        )
        if {"nit", "t3-auto"} - _issue_labels(host.get_issue(issue_url)):
            message = "NIT follow-up issue labels did not read back"
            raise NitLaneError(message)


_NIT_ROW = re.compile(r"^\d+\. \[(.+):(\d+)\]\((https?://[^)]+)\) — (.+)$")


def _canonical_follow_up_body(*, pr_url: str, marker: str, existing: str, new: tuple[NitPoint, ...]) -> str:
    header = f"Non-blocking points from {pr_url} {marker}"
    closing = "These points do not block the merge and are tracked in this issue."
    if not existing.splitlines() or existing.splitlines()[0] != header:
        message = "Existing NIT follow-up issue has an unexpected header"
        raise NitLaneError(message)
    points: dict[tuple[str, int, str, str], NitPoint] = {}
    for line in existing.splitlines()[1:]:
        match = _NIT_ROW.fullmatch(line)
        if match:
            file, number, permalink, summary = match.groups()
            point = NitPoint(file, int(number), summary, permalink)
            points[point.file, point.line, point.summary, point.permalink] = point
        elif line and line not in {closing, header} and not line.startswith(("## Request added", "<!-- t3-hygiene:")):
            message = "Existing NIT follow-up issue has content outside its tracked points"
            raise NitLaneError(message)
    for point in new:
        points[point.file, point.line, point.summary, point.permalink] = point
    return render_follow_up_body(pr_url=pr_url, marker=marker, points=tuple(points.values()))


def _issue_labels(issue: "RawAPIDict") -> set[str]:
    raw = issue.get("labels")
    if not isinstance(raw, list):
        return set()
    return {str(label.get("name") or "") if isinstance(label, dict) else str(label) for label in raw}


def _create_follow_up_issue(
    host: "CodeHostBackend",
    ref: "PrRef",
    *,
    thread_ids: tuple[str, ...],
    body: str,
    marker: str,
) -> str:
    draft = IssueDraft(
        repo=ref.slug,
        title=f"Non-blocking points from PR #{ref.pr_id}",
        body=body,
        labels=("nit", "t3-auto"),
        action="nit_follow_up_create",
        forge=ref.host_kind,
        merge_request_to_resolve_discussions_of=ref.pr_id
        if ref.host_kind == "gitlab" and len(thread_ids) == 1 and thread_ids[0] != "issue-only"
        else None,
        discussion_to_resolve=thread_ids[0]
        if ref.host_kind == "gitlab" and len(thread_ids) == 1 and thread_ids[0] != "issue-only"
        else "",
    )
    outcome = create_or_extend_by_marker(host=host, draft=draft, marker=marker)
    if outcome.kind not in {"created_new", "extended_existing"} or not outcome.issue_url:
        message = "NIT follow-up issue was not created or extended"
        raise NitLaneError(message)
    return outcome.issue_url


def _resolve_threads(host: "CodeHostBackend", context: _NitContext, thread_ids: tuple[str, ...]) -> None:
    ref = context.ref
    for thread_id in thread_ids:
        if thread_id == "issue-only":
            continue
        current = host.list_review_threads(repo=ref.slug, pr_iid=ref.pr_id)
        if any(thread.get("id") == thread_id and not _resolved(thread) for thread in current):

            def resolve(target_thread_id: str = thread_id) -> "RawAPIDict":
                result = host.resolve_review_thread(repo=ref.slug, pr_iid=ref.pr_id, thread_id=target_thread_id)
                if result.get("error"):
                    message = f"NIT review thread resolution refused: {result['error']}"
                    raise NitLaneError(message)
                return result

            require_on_behalf_approval(
                target=f"{ref.slug}#{ref.pr_id}",
                action="resolve_review_nit",
                publish=resolve,
                context=OnBehalfContext(own_mr=context.own_pr, target=ref.slug),
            )
    current = host.list_review_threads(repo=ref.slug, pr_iid=ref.pr_id)
    states = {str(thread.get("id")): _resolved(thread) for thread in current}
    if any(states.get(thread_id) is not True for thread_id in thread_ids if thread_id != "issue-only"):
        message = "NIT review thread resolution did not read back"
        raise NitLaneError(message)


def track_nits(*, host: "CodeHostBackend", pr_url: str, head_sha: str, points: tuple[NitPoint, ...]) -> NitReceipt:
    for point in points:
        if refusal := comment_refusal(f"Nit: {point.summary}", general=False):
            message = f"{COMMENT_CHECK_PREFIX}{refusal}"
            raise NitLaneError(message)
    try:
        context = _target(host, pr_url, head_sha, points)
        paired, thread_ids = _paired_comments(host, context, points)
        issue_url = _follow_up_issue(host, context, paired=paired, thread_ids=thread_ids)
        _resolve_threads(host, context, thread_ids)
    except OutboundBlockedError as exc:
        raise NitLaneError(str(exc)) from exc
    return NitReceipt(issue_url, thread_ids, head_sha)
