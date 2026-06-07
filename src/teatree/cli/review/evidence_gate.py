"""Structured-evidence pre-publish gate for review findings (souliane/teatree#1280).

When a reviewer posts a finding via :class:`teatree.cli.review.ReviewService`
whose body matches a ``"X is missing/wrong/broken"`` pattern, the gate refuses
the post unless an accompanying :class:`FindingEvidence` record carries the
typed receipts the reviewer used to derive the claim.

Sibling gates on the same publishing flow:

* :mod:`teatree.cli.review.on_behalf` — recorded-approval gate (#960).
* :mod:`teatree.cli.review.shape_gate` — colleague-MR shape gate (#1114).
* :mod:`teatree.cli.review.todo_gate` — author-marked TODO/FIXME anchor gate (#1186).

All four run on every publishing call that takes a body, in this order:
on-behalf → shape → TODO-anchor → evidence. The evidence gate is the last one
because it is the most expensive to satisfy (the reviewer must do the master
check / ticket-dep scan / helper indirection lookup work) — earlier gates
should refuse the cheap mistakes first.

Refusal contract (mirrors the sibling gates):

* The gate function returns ``""`` to proceed, or a non-empty refusal string
    to refuse. The caller short-circuits the GitLab API call with
    ``(message, 1)`` — same shape every other gate uses.
* The refusal names the schema (``FindingEvidence``), the missing field, and
    the recovery path so the agent knows exactly how to satisfy the gate.

Schema extension path:

The schema starts with a minimal set of fields covering the four common
review-finding shapes named in #1280 (missing file, missing function, wrong
API signature, stale documentation). New fields are additive — extend
:class:`FindingEvidence` with default ``[]`` / ``""`` values, then teach
:func:`check_finding_evidence` to consult them where appropriate. The
existing fields never change shape (no rename, no list→dict, no required→
optional flip) because they are the public CLI surface — instead, add new
fields alongside.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Literal

# Confidence values the schema accepts. ``verified`` is the only value that
# can pass the gate; ``speculative`` is documented so the agent has a way to
# express "I think this is missing but I have not checked master" — and then
# the gate drops the finding entirely (silence on a checked claim is correct,
# per the issue body).
Confidence = Literal["verified", "speculative"]
_ALLOWED_CONFIDENCE: frozenset[str] = frozenset({"verified", "speculative"})

# Pattern detection. Anchored on word boundaries so an incidental
# "the brokerage" or "wrongdoer" does not trip. Two sub-patterns:
#
# 1. ``<noun> is/are (missing|wrong|broken|stale)`` — the canonical issue-body
#    shape ("the helper is missing", "the API signature is wrong").
# 2. Standalone negation-of-existence phrases — "does not exist", "cannot find",
#    "there is no", "should not exist", "no such", "missing from" — which
#    convey the same claim shape without the copula.
_EVIDENCE_CLAIM_RE = re.compile(
    r"\b(?:"
    r"is\s+(?:missing|wrong|broken|stale|incorrect)|"
    r"are\s+(?:missing|wrong|broken|stale|incorrect)|"
    r"does\s+not\s+exist|"
    r"do\s+not\s+exist|"
    r"doesn'?t\s+exist|"
    r"don'?t\s+exist|"
    r"cannot\s+find|"
    r"can'?t\s+find|"
    r"there\s+is\s+no\s+(?:such\s+)?[a-z_]+|"
    r"should\s+not\s+exist|"
    r"shouldn'?t\s+exist|"
    r"no\s+such\s+(?:function|method|symbol|file|module|helper|class)|"
    r"missing\s+from|"
    r"stale\s+reference"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FindingEvidence:
    """Typed evidence record for a review finding (souliane/teatree#1280).

    Each field captures one of the receipts a reviewer used to derive an
    "X is missing/wrong/broken" claim. The combination is consumed by
    :func:`check_finding_evidence` to decide whether the publish is allowed.

    Field semantics:

    ``master_check_paths``
        Files (optionally with ``:LINE`` or ``:START-END`` suffix) the
        reviewer inspected on ``origin/<default>`` before concluding the
        thing was absent or wrong. Example: ``"src/teatree/cli/foo.py:42"``.

    ``ticket_dep_refs``
        Ticket IDs the reviewed MR explicitly cited as upstream
        dependencies that should already carry the missing piece. Example:
        ``["souliane/teatree#1234"]``.

    ``helper_indirection_paths``
        Helper modules the reviewer consulted to confirm the consumer
        reads the canonical list/registry by indirection (so an apparent
        local miss does not contradict a non-local registration). May be
        empty when no helper indirection applies.

    ``recent_merge_sweep_query``
        The literal ``git log --grep`` / ``gh pr list`` / ``glab mr list``
        query the reviewer ran to confirm no recent sibling merge added
        the symbol since the MR's base. May be empty when not applicable
        (e.g. when ``master_check_paths`` already pins the receipt).

    ``confidence``
        ``"verified"`` (the reviewer ran the checks and they support the
        claim) or ``"speculative"`` (the reviewer thinks something is
        wrong but has not verified). Only ``"verified"`` ever passes the
        gate — ``"speculative"`` is documented as an explicit escape
        hatch for the agent to express uncertainty, and the gate drops
        the finding when it sees this value.

    Extension path (per #1280 "extensible — start with a minimal field
    set"): add new fields with default values — never change the shape
    of these five. See module docstring.
    """

    master_check_paths: list[str] = field(default_factory=list)
    ticket_dep_refs: list[str] = field(default_factory=list)
    helper_indirection_paths: list[str] = field(default_factory=list)
    recent_merge_sweep_query: str = ""
    confidence: Confidence = "verified"

    @classmethod
    def from_json(cls, raw: str) -> "FindingEvidence":
        """Construct from a JSON string (the CLI flag plumbing path).

        Raises :class:`ValueError` when ``raw`` is not valid JSON, is not
        a JSON object, or names a ``confidence`` outside the
        ``verified|speculative`` literal set. Unknown keys are ignored
        (forward-compat with future schema extensions).
        """
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            msg = f"FindingEvidence.from_json: invalid json: {e}"
            raise ValueError(msg) from e
        if not isinstance(obj, dict):
            msg = "FindingEvidence.from_json: invalid json: expected a JSON object"
            raise ValueError(msg)  # noqa: TRY004 — uniform ValueError surface for CLI plumbing
        confidence_raw = obj.get("confidence", "verified")
        if confidence_raw not in _ALLOWED_CONFIDENCE:
            msg = (
                f"FindingEvidence.from_json: confidence={confidence_raw!r} is not one of {sorted(_ALLOWED_CONFIDENCE)}"
            )
            raise ValueError(msg)
        return cls(
            master_check_paths=_string_list(obj.get("master_check_paths", [])),
            ticket_dep_refs=_string_list(obj.get("ticket_dep_refs", [])),
            helper_indirection_paths=_string_list(obj.get("helper_indirection_paths", [])),
            recent_merge_sweep_query=str(obj.get("recent_merge_sweep_query", "")),
            confidence=confidence_raw,
        )


def _string_list(value: object) -> list[str]:
    """Coerce a JSON list value to ``list[str]`` (drop non-strings rather than crash)."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def looks_like_evidence_claim(body: str) -> bool:
    """Whether ``body`` reads as an 'X is missing/wrong/broken' claim.

    True when the body matches any of the canonical claim phrases — the
    pattern set is biased to flag (a false-positive costs the reviewer
    one structured evidence record; a false-negative recurs the #1280
    failure mode).
    """
    if not body:
        return False
    return _EVIDENCE_CLAIM_RE.search(body) is not None


def check_finding_evidence(*, body: str, evidence: object) -> str:
    """Return a non-empty refusal when a claim-shaped post lacks valid evidence.

    Returns ``""`` (proceed) when any of these hold:

    * ``body`` is empty (other gates own the empty-body refusal).
    * ``body`` does not look like an "X is missing/wrong/broken" claim
        (no schema needed for a nit or a non-claim observation).
    * ``evidence`` is a :class:`FindingEvidence` with ``confidence ==
        "verified"`` AND at least one of ``master_check_paths`` or
        ``ticket_dep_refs`` is non-empty.

    Returns an actionable refusal string otherwise. The caller short-
    circuits the GitLab API call with ``(message, 1)`` — same shape every
    sibling gate uses.

    The ``evidence`` parameter is typed ``object`` rather than
    ``FindingEvidence | None`` so the CLI plumbing can pass either a
    dataclass instance or ``None`` without a type-narrow at every
    caller. Anything that is not a :class:`FindingEvidence` is treated
    as "no evidence supplied".
    """
    if not body:
        return ""
    if not looks_like_evidence_claim(body):
        return ""
    if not isinstance(evidence, FindingEvidence):
        return _refusal_missing()
    if evidence.confidence == "speculative":
        return _refusal_speculative()
    if not evidence.master_check_paths and not evidence.ticket_dep_refs:
        return _refusal_empty_signals()
    return ""


def _refusal_missing() -> str:
    """Refusal when no evidence record was supplied at all."""
    return (
        "Refusing 'missing/wrong/broken' finding without evidence (souliane/teatree#1280):\n"
        "The body asserts something is missing, wrong, or broken — a structured\n"
        'FindingEvidence record is required. Pass --evidence-json \'{"master_check_paths":\n'
        '[...], "ticket_dep_refs": [...], "confidence": "verified"}\' on the CLI, or\n'
        "downgrade the finding to a non-claim observation (a nit, a question, a\n"
        "cross-reference). Speculative findings on master-state should drop entirely —\n"
        "silence on a checked claim is correct.\n"
        "Schema: teatree.cli.review.evidence_gate.FindingEvidence.\n"
        "See: https://github.com/souliane/teatree/issues/1280"
    )


def _refusal_speculative() -> str:
    """Refusal when ``confidence == 'speculative'``."""
    return (
        "Refusing speculative 'missing/wrong/broken' finding (souliane/teatree#1280):\n"
        "FindingEvidence(confidence='speculative') means the reviewer has not verified\n"
        "the claim against master, the ticket dependencies, or a recent-merge sweep.\n"
        "Speculative findings drop entirely — silence on a checked claim is correct.\n"
        "To publish: re-run the checks, populate master_check_paths or ticket_dep_refs,\n"
        "and set confidence='verified'."
    )


def _refusal_empty_signals() -> str:
    """Refusal when ``confidence='verified'`` but both signal lists are empty."""
    return (
        "Refusing 'missing/wrong/broken' finding with empty evidence signals\n"
        "(souliane/teatree#1280):\n"
        "FindingEvidence.confidence='verified' but BOTH master_check_paths and\n"
        "ticket_dep_refs are empty. At least one must carry an entry:\n"
        "  - master_check_paths: file(:line) on origin/<default> you inspected, or\n"
        "  - ticket_dep_refs: ticket IDs the MR cites as upstream dependencies.\n"
        "A 'verified' tag without either signal is not evidence — it is an assertion\n"
        "without receipts, which is the #1280 failure mode."
    )
