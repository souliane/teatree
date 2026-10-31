"""``t3 <overlay> review hand-merge`` — record the verdict a reviewer returned as prose.

Every check runs before the first write, in this order: the envelope extracted from the reply,
its keys, shape and enums, the NIT comment check, the head binding, the order text. A refusal
leaves no ``ReviewVerdict`` row; only a fully checked envelope reaches ``record_result``.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.agents.result_schema import ReviewVerdictEnvelope
from teatree.agents.result_schema_review import REVIEW_VERDICT_SCHEMA_PROPERTY, review_envelope_shape_error
from teatree.agents.runner_result import ResultEnvelopeError, parse_result
from teatree.core.management.commands._review_impl import RecordRequest, RecordResult, record_result
from teatree.core.merge.host_kind import forge_for_repo_slug, pr_web_url_for_repo_slug
from teatree.core.review.nit_lane import nit_comment_error
from teatree.core.review.verdict_head_binding import resolve_verdict_head
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from django_typer.management import TyperCommand

STALE_ORDER_PREFIX = "STALE_ORDER: "

_NOT_APPLIED_KEYS = ("rubric_grades", "hold_dispositions", "evidence_gaps")
_SHA_TOKEN = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.IGNORECASE)
_PR_TOKEN = re.compile(r"(?:\b(?:PR|MR)\s*|[#!]|/pull/|/merge_requests/)(\d+)\b", re.IGNORECASE)
_ISSUE_TOKEN = re.compile(r"\b(?:issue|ticket)s?\s+#\d+\b", re.IGNORECASE)
_ENVELOPE_SCHEMA = {**REVIEW_VERDICT_SCHEMA_PROPERTY, "additionalProperties": False}
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


class HandMergeRefusal(TypedDict):
    error: str


class HandMergeRefusedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class HandMergeRequest:
    pr_id: int
    slug: str
    envelope_file: str
    order_text: str


def schema_violation(value: object, schema: Mapping[str, object], *, path: str) -> str:
    expected = schema.get("type")
    if isinstance(expected, str) and not _JSON_TYPES[expected](value):
        return f"{path} must be a JSON {expected}"
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        return f"{path} must be one of {allowed}, got {value!r}"
    if isinstance(value, dict):
        return _object_violation(value, schema, path=path)
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        return next(
            (
                error
                for index, item in enumerate(value)
                if (error := schema_violation(item, items, path=f"{path}[{index}]"))
            ),
            "",
        )
    return ""


def _object_violation(value: Mapping[str, object], schema: Mapping[str, object], *, path: str) -> str:
    properties = cast("Mapping[str, Mapping[str, object]]", schema.get("properties", {}))
    required = cast("list[str]", schema.get("required", []))
    if missing := [key for key in required if key not in value]:
        return f"{path} is missing {missing}"
    if schema.get("additionalProperties") is False and (unknown := sorted(set(value) - set(properties))):
        return f"{path} carries unknown key(s) {unknown}"
    return next(
        (
            error
            for key, sub_schema in properties.items()
            if key in value and (error := schema_violation(value[key], sub_schema, path=f"{path}.{key}"))
        ),
        "",
    )


def _checked_envelope(request: HandMergeRequest) -> ReviewVerdictEnvelope:
    if not request.envelope_file.strip():
        message = "--envelope-file is required: the file holding the reviewer's raw reply"
        raise HandMergeRefusedError(message)
    try:
        reply = Path(request.envelope_file).read_text(encoding="utf-8")
        blob = parse_result(reply)
    except (OSError, ResultEnvelopeError) as exc:
        raise HandMergeRefusedError(str(exc)) from exc
    raw = blob.get("review_verdict")
    if not isinstance(raw, dict):
        message = f"no review_verdict envelope found in {request.envelope_file}"
        raise HandMergeRefusedError(message)
    envelope = cast("ReviewVerdictEnvelope", raw)
    if error := (
        schema_violation(envelope, _ENVELOPE_SCHEMA, path="review_verdict")
        or review_envelope_shape_error(envelope)
        or nit_comment_error(
            envelope.get("findings"), pr_url_of=partial(pr_web_url_for_repo_slug, request.slug, request.pr_id)
        )
    ):
        raise HandMergeRefusedError(error)
    return envelope


def _live_head(request: HandMergeRequest, envelope: ReviewVerdictEnvelope) -> str:
    binding = resolve_verdict_head(
        asserted=str(envelope.get("reviewed_sha", "")),
        dispatch_head="",
        pr=PrRef(slug=request.slug, pr_id=request.pr_id, host_kind=forge_for_repo_slug(request.slug) or "github"),
    )
    if binding.error:
        raise HandMergeRefusedError(binding.error)
    return binding.head


def _require_live_order(request: HandMergeRequest, head: str) -> None:
    cited_shas = [sha for sha in _SHA_TOKEN.findall(request.order_text) if not head.lower().startswith(sha.lower())]
    cited_prs = [
        f"#{pr}" for pr in _PR_TOKEN.findall(_ISSUE_TOKEN.sub("", request.order_text)) if int(pr) != request.pr_id
    ]
    if stale := [*cited_shas, *cited_prs]:
        message = (
            f"{STALE_ORDER_PREFIX}the order text cites {', '.join(stale)} but {request.slug}#{request.pr_id} "
            f"points at {head[:12]}; re-issue the order against the live head"
        )
        raise HandMergeRefusedError(message)


def hand_merge(command: "TyperCommand", request: HandMergeRequest) -> tuple[RecordResult | HandMergeRefusal, str]:
    try:
        envelope = _checked_envelope(request)
        head = _live_head(request, envelope)
        _require_live_order(request, head)
    except HandMergeRefusedError as refusal:
        return {"error": str(refusal)}, f"  hand-merge refused: {refusal}"
    payload, human = record_result(
        command,
        RecordRequest(
            pr_id=request.pr_id,
            slug=request.slug,
            reviewed_sha=head,
            verdict=str(envelope["verdict"]),
            reviewer_identity=str(envelope.get("reviewer_identity", "")),
            gh_verify_result=str(envelope.get("gh_verify_result", "green")),
            blast_class=str(envelope.get("blast_class", "logic")),
            findings_json=json.dumps(envelope.get("findings", [])),
            merge_result_retake=bool(envelope.get("merge_result_retake")),
        ),
    )
    not_applied = [key for key in _NOT_APPLIED_KEYS if envelope.get(key)]
    note = f"\n  not applied by hand-merge: {', '.join(not_applied)}" if not_applied else ""
    return payload, f"{human}{note}"
