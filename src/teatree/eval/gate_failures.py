"""Gate-failure feedback loop: turn a recurring preventable gate failure into a green eval (#2024).

A non-zero hook exit in a session transcript IS a gate failure (a prek hook
block, a comment-density / banned-terms / doc-update gate). The user's directive:
every *preventable* gate failure — one the agent should never have produced —
must drive an AI eval that reproduces the scenario and asserts the agent's output
passes the gate first-try, so the gate stops being hit by trial-and-error.

This module is the deterministic half of that loop:

``extract_gate_failures`` reads the SINGLE transcript chokepoint
(``session_transcript.extract_hook_events``) and keeps only the non-zero exits.
There is no per-gate instrumentation, one reader over the on-disk session JSONL.

``GateFailure`` is a frozen record carrying ONLY the gate name, the neutralized
command shape, and the session id. It NEVER carries the hook's ``stdout`` /
``stderr``, those hold the diff/banned content the gate was reacting to, the very
leak vector. The fingerprint hashes (gate, normalized command shape) so two
comment-density fails on different files hash together while a different gate
hashes apart.

``classify_gate_failure`` is a declarative table: a fixed set of environmental
gates (dependency audit/lock/sync, secret scan, upstream type / module-graph
checkers) are out of the agent's hands; everything else, agent-output-shaped, is
preventable, and an unknown gate is preventable too (fail toward an eval).

``record_gate_failures`` / ``escalate_gate_failures`` reuse the review-findings
durable store and dedup-aware filer so a recurring preventable failure files one
scoped, banned-terms-safe enforcement issue, deduped by fingerprint.

Dependency direction: this lives in ``teatree.eval`` (layer ``integration``),
NOT ``teatree.core`` (layer ``domain``) — a ``core -> eval`` import is a
backwards tach edge. ``teatree.eval`` already depends on ``teatree.core``, so it
reuses ``core.review_findings``'s fingerprint/store/file primitives on a forward
edge; the ``retro gate-failures`` CLI command lives in ``teatree.core.management``
(layer ``interface``) and calls into here on another forward edge.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from teatree.core.review_findings import (
    ClassifiedFinding,
    FiledIssue,
    FilingContext,
    FindingClass,
    FindingsStore,
    ReviewFinding,
    file_class_c_issue,
)
from teatree.eval.session_transcript import SessionEvent, extract_hook_events
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

_PATH_RE = re.compile(r"\S*/\S+|\b\S+\.[A-Za-z0-9_]+\b")
_WHITESPACE_RE = re.compile(r"\s+")
_PASSING_EXIT_CODES: frozenset[int | None] = frozenset({None, 0})
"""Hook exit codes that are NOT a gate failure: ``0`` (allow) and no exit recorded."""


class GateVerdict(StrEnum):
    """Whether a gate failure was the agent's to prevent.

    ``PREVENTABLE`` — agent-output-shaped (comment density, banned terms, a doc
    gate); the agent should never have produced the violating output, so it
    warrants an eval. ``ENVIRONMENTAL`` — outside the agent's output (a
    dependency audit/lock, a secret scan, an upstream type/module-graph checker);
    an eval would not change the outcome.
    """

    PREVENTABLE = "preventable"
    ENVIRONMENTAL = "environmental"


_ENVIRONMENTAL_GATES: frozenset[str] = frozenset(
    {
        "uv-audit",
        "uv-lock",
        "uv-sync",
        "gitleaks",
        "ty",
        "ty-check",
        "tach",
        "import-linter",
    }
)
"""Gates whose failure is environmental — not agent-output-shaped.

Dependency audit/lock/sync, the secret scanner, and the upstream type /
module-graph conformance checkers. A failure here reflects the dependency tree or
the import graph, not a piece of output the agent chose to write, so an eval
cannot make it pass first-try. Everything not in this set is treated as
preventable (fail toward an eval), including an unknown gate.
"""


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One non-zero hook exit from a session transcript.

    Carries ONLY the gate name, the hook event, the neutralized command shape,
    and the session id — never the hook's ``stdout``/``stderr``, which hold the
    diff/banned content the gate was reacting to (the leak vector). The
    fingerprint hashes (gate, normalized command shape) so two failures of the
    same gate on different files hash together (one recurring class) while a
    different gate hashes apart.
    """

    gate: str
    hook_event: str
    command: str
    session_id: str

    @property
    def command_shape(self) -> str:
        """The command with file paths and extra whitespace stripped to a stable shape."""
        stripped = _PATH_RE.sub("", self.command)
        return _WHITESPACE_RE.sub(" ", stripped).strip().lower()

    @property
    def fingerprint(self) -> str:
        """The dedup key: a stable hash of (gate, normalized command shape).

        Delegates to the :class:`~teatree.core.review_findings.ReviewFinding`
        adapter so the value recorded in the durable store and the value the
        escalation filer dedups on are ONE identical fingerprint — no second
        hashing scheme to drift.
        """
        return _as_finding(self).fingerprint

    def as_dict(self) -> RawAPIDict:
        """A privacy-safe serialization — gate + neutralized shape only, no stdout/stderr."""
        return {
            "gate": self.gate,
            "hook_event": self.hook_event,
            "command_shape": self.command_shape,
            "session_id": self.session_id,
            "fingerprint": self.fingerprint,
        }


def extract_gate_failures(events: list[SessionEvent], *, session_id: str) -> list[GateFailure]:
    """Filter the single hook-event chokepoint down to the non-zero exits.

    A hook with ``hook_exit_code`` ``None`` (no exit recorded) or ``0`` (allow)
    is not a failure. Every other exit is a gate failure. The gate identity
    (``hookName``) and the ``command`` are read from the raw attachment; the gate
    name distinguishes a comment-density block from a banned-terms block (the
    ``SessionEvent.hook_event`` is the coarser PreToolUse / Stop bucket). The
    privacy-sensitive ``stdout``/``stderr`` on the attachment are never copied
    onto the record.
    """
    failures: list[GateFailure] = []
    for event in extract_hook_events(events):
        if event.hook_exit_code in _PASSING_EXIT_CODES:
            continue
        attachment = event.raw.get("attachment")
        attachment = attachment if isinstance(attachment, dict) else {}
        failures.append(
            GateFailure(
                gate=_str_field(attachment, "hookName") or (event.hook_event or ""),
                hook_event=event.hook_event or "",
                command=_str_field(attachment, "command"),
                session_id=session_id,
            )
        )
    return failures


def _str_field(attachment: RawAPIDict, key: str) -> str:
    value = attachment.get(key)
    return value if isinstance(value, str) else ""


def classify_gate_failure(failure: GateFailure) -> GateVerdict:
    """Classify a failure ``preventable`` / ``environmental`` via the declarative table.

    A gate in :data:`_ENVIRONMENTAL_GATES` is environmental; everything else,
    including an unknown gate, is preventable — fail toward an eval.
    """
    if failure.gate in _ENVIRONMENTAL_GATES:
        return GateVerdict.ENVIRONMENTAL
    return GateVerdict.PREVENTABLE


def record_gate_failures(store: FindingsStore, failures: list[GateFailure]) -> None:
    """Persist each failure's fingerprint to the durable store so recurrence is observable.

    Reuses the review-findings per-key store under a synthetic per-session key
    (``gate-failure-session:<id>``) so the same recurring fingerprint across
    sessions surfaces via :meth:`FindingsStore.recurring_fingerprints`. Each
    failure is recorded as class C — the gate-failure equivalent of a recurring
    enforcement gap.
    """
    for failure in failures:
        classified = ClassifiedFinding(finding=_as_finding(failure), classification=FindingClass.C)
        store.record(_session_key(failure.session_id), [classified])


def escalate_gate_failures(
    host: "CodeHostBackend",
    *,
    failures: list[GateFailure],
    store: FindingsStore,
    context: FilingContext,
) -> list[FiledIssue]:
    """File one deduped enforcement issue per recurring preventable failure.

    A failure is escalated only when it is both *preventable* (classifier) and
    *recurring* (its fingerprint recorded across >= 2 sessions). The filing
    reuses :func:`~teatree.core.review_findings.file_class_c_issue`, so it is
    deduped by fingerprint marker (a re-run never refiles), banned-terms-safe
    (a hit withholds rather than leaks), and clickable-link safe. Environmental
    or non-recurring failures file nothing.
    """
    recurring = store.recurring_fingerprints(min_occurrences=2)
    filed: list[FiledIssue] = []
    seen: set[str] = set()
    for failure in failures:
        finding = _as_finding(failure)
        if finding.fingerprint in seen:
            continue
        if classify_gate_failure(failure) is not GateVerdict.PREVENTABLE:
            continue
        if finding.fingerprint not in recurring:
            continue
        seen.add(finding.fingerprint)
        filed.append(
            file_class_c_issue(
                host,
                finding=finding,
                enforcement=_enforcement_note(failure),
                context=context,
            )
        )
    return filed


def _session_key(session_id: str) -> str:
    return f"gate-failure-session:{session_id}"


def _as_finding(failure: GateFailure) -> ReviewFinding:
    """Adapt a :class:`GateFailure` to the :class:`ReviewFinding` the filer expects.

    The fingerprint must match :attr:`GateFailure.fingerprint` so dedup keys on
    the same value, so the finding's body + path + line are derived from the
    gate + command shape (a path-free, banned-content-free description).
    """
    body = f"preventable gate `{failure.gate}` fired on `{failure.command_shape}`"
    return ReviewFinding(body=body, path="", line=0, author="gate-failure-loop")


def _enforcement_note(failure: GateFailure) -> str:
    return (
        f"preventable gate `{failure.gate}` fired recurrently. The smallest "
        "anti-vacuous eval to stop it first-try: a behavioral scenario whose "
        "prompt reproduces the violating output shape and whose matcher asserts "
        "the agent's output passes the gate (with a `_fail` fixture that goes RED)."
    )
