"""Rewrite the envelope key drifts reviewers were measured returning onto the schema's own names.

Only unambiguous renames and lossless re-shapes: a value is moved or rendered, never inferred.
A drift that would need a guess is left as returned, so the recorder refuses it by name and the
reviewer gets its corrective retry.
"""

import json

from teatree.agents.result_schema import AgentResultBlob


def normalize_envelope_aliases(result: AgentResultBlob) -> AgentResultBlob:
    normalized = dict(result)
    _rename_work_item_url(normalized)
    _nest_rubric_grades(normalized)
    _render_ac_coverage_list(normalized)
    return normalized


def _rename_work_item_url(result: AgentResultBlob) -> None:
    context = result.get("review_context")
    if isinstance(context, dict) and "work_item_url" in context and "work_item" not in context:
        renamed = {key: value for key, value in context.items() if key != "work_item_url"}
        result["review_context"] = {**renamed, "work_item": context["work_item_url"]}


def _nest_rubric_grades(result: AgentResultBlob) -> None:
    verdict = result.get("review_verdict")
    if "rubric_grades" in result and isinstance(verdict, dict) and "rubric_grades" not in verdict:
        result["review_verdict"] = {**verdict, "rubric_grades": result.pop("rubric_grades")}


def _render_ac_coverage_list(result: AgentResultBlob) -> None:
    attestation = result.get("anti_vacuity")
    if isinstance(attestation, dict) and isinstance(coverage := attestation.get("ac_coverage"), list) and coverage:
        result["anti_vacuity"] = {**attestation, "ac_coverage": json.dumps(coverage)}
