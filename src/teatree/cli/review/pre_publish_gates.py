"""The pre-publish gate chain for ``ReviewService`` posting methods.

Extracted from :mod:`teatree.cli.review.service` (mirroring the
:mod:`teatree.cli.review.post_impl` extraction) so that module stays
under the OOP/LOC ceiling (``scripts/hooks/check_module_health.py``).

The chain runs, in order, the gates that refuse a colleague-visible (or
draft) review post before any GitLab API call:

on-behalf (#960) → colleague-MR shape (#1114) → comment-bloat (#2663) →
multi-finding general-note (#72) → stay-inline-once-inline (PR-08) →
author-marked TODO-anchor (#1186) → structured-evidence (#1280).

Each gate is a pure ``check_*`` function in its own sibling module
returning a non-empty steering string to refuse, or ``""`` to proceed.
``run_pre_publish_gates`` returns the first refusal it hits, or ``""``
when every gate passes.

``allow_long_review`` / ``allow_todo_blocker`` / ``force_general`` /
``allow_bloat`` are the #126 per-call escapes for the shape, TODO-anchor,
multi-finding general-note, and comment-bloat gates respectively; none
relaxes the on-behalf or evidence gates.
"""
# ruff: noqa: SLF001 — sibling-module extraction of the ReviewService gate chain (#2663).

from typing import TYPE_CHECKING

from teatree.cli.review.bloat_gate import check_review_bloat
from teatree.cli.review.evidence_gate import FindingEvidence, check_finding_evidence
from teatree.cli.review.general_inline_gate import check_general_inline_findings
from teatree.cli.review.inline_shape_gate import check_inline_shape
from teatree.cli.review.on_behalf import check_on_behalf
from teatree.cli.review.shape_gate import check_review_shape
from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor

if TYPE_CHECKING:
    from teatree.cli.review.service import ReviewService


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_pre_publish_gates(  # noqa: PLR0913 — orchestration entry-point: each kwarg is a documented gate input (MR coordinate + body + anchor + action + evidence + the #126 escapes).
    service: "ReviewService",
    *,
    repo: str,
    mr: int,
    note: str,
    file: str,
    line: int,
    action: str,
    evidence: FindingEvidence | None,
    allow_long_review: bool = False,
    allow_todo_blocker: bool = False,
    force_general: bool = False,
    allow_bloat: bool = False,
) -> str:
    """Run the pre-publish gate chain; return the first refusal or ``""``.

    See the module docstring for the chain order and the per-call escape
    semantics. *service* supplies ``_get_api`` so the network-touching
    gates (shape, TODO-anchor) can fetch MR metadata.
    """
    encoded = repo.replace("/", "%2F")
    api = service._get_api()
    inline = bool(file and line)
    # Each gate is a zero-arg closure returning a refusal string or "". The
    # chain returns the first non-empty refusal — one return point keeps the
    # orchestration under the return-count ceiling as gates are added.
    gates = (
        lambda: check_on_behalf(repo, mr, action),
        lambda: check_review_shape(
            api=api, encoded_repo=encoded, mr=mr, body=note, inline=inline, allow_long_review=allow_long_review
        ),
        lambda: check_review_bloat(body=note, allow_bloat=allow_bloat),
        lambda: check_general_inline_findings(body=note, inline=inline, force_general=force_general),
        lambda: check_inline_shape(api=api, encoded_repo=encoded, mr=mr, inline=inline, force_general=force_general),
        lambda: check_todo_anchor(
            api=api,
            encoded_repo=encoded,
            mr=mr,
            body=note,
            anchor=InlineAnchor(file=file, line=line),
            allow_todo_blocker=allow_todo_blocker,
        ),
        lambda: check_finding_evidence(body=note, evidence=evidence),
    )
    for gate in gates:
        refusal = gate()
        if refusal:
            return refusal
    return ""
