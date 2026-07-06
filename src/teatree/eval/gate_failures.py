"""Gate-failure feedback loop: turn a recurring preventable gate failure into a green eval (#2024).

A teatree quality gate firing on the agent's own output (the structured-question
Stop gate, comment-density, banned-terms, doc-update) lands in the on-disk session
transcript as a hook error attachment. The user's directive: every *preventable*
gate failure — one the agent should never have produced — must drive an AI eval
that reproduces the scenario and asserts the agent's output passes the gate
first-try, so the gate stops being hit by trial-and-error.

This module is the deterministic half of that loop, built against the REAL
Claude Code on-disk session schema (verified against
``~/.claude/projects/*/*.jsonl``):

A gate BLOCK is ``attachment.type == "hook_blocking_error"``. It carries NO
``exitCode``; the gate identity lives in ``attachment.blockingError.blockingError``,
whose text leads with a ``TEATREE GATE — <phrase>`` marker. ``hookName`` is the
EVENT:TOOL label ("Stop", "PreToolUse:Bash"), never a gate name, and ``command``
is the same runner invocation across every gate — neither identifies the gate.

``TEATREE LOOP SELF-PUMP`` is also a ``hook_blocking_error`` but is a
continue-the-loop signal, not a gate failure — excluded by marker.

A ``hook_non_blocking_error`` carries ``exitCode:1`` and is an infra/dependency
failure (a missing plugin dir, a traceback) — surfaced and classified
environmental.

Detection therefore keys on the attachment TYPE plus the blockingError marker,
NEVER on ``exitCode`` (a blocking error has none). The gate identity is a minimal
normalized SLUG (:func:`gate_identity_slug`) derived from the gate's own fixed
marker phrase — NOT the agent's output, NOT a diff, NOT PII. :class:`GateFailure`
stores ONLY that slug + the session id; never the raw blockingError message, the
``stderr``, the ``command``, or ``stdout`` (the leak vectors).

``classify_gate_failure`` is a declarative table: an environmental failure (an
infra/dependency/tooling breakage) is out of the agent's hands; everything else,
agent-output-shaped, is preventable, and an unknown gate is preventable too (fail
toward an eval).

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
from typing import TYPE_CHECKING, cast

from teatree.core.review.review_findings import (
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

_GATE_MARKER = "TEATREE GATE"
_SELF_PUMP_MARKER = "TEATREE LOOP SELF-PUMP"
_BLOCKING_ERROR_TYPE = "hook_blocking_error"
_NON_BLOCKING_ERROR_TYPE = "hook_non_blocking_error"

_MARKER_STRIP_RE = re.compile(r"^\s*teatree\s+(?:gate|loop)\s*[—\-:]*\s*", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"[.;:\n]")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_WORD_CAP = 6
_SLUG_CHAR_CAP = 80


class GateVerdict(StrEnum):
    """Whether a gate failure was the agent's to prevent.

    ``PREVENTABLE`` — agent-output-shaped (the inline-question Stop gate, comment
    density, banned terms, a doc gate); the agent should never have produced the
    violating output, so it warrants an eval. ``ENVIRONMENTAL`` — outside the
    agent's output (a missing plugin dir, a dependency/tooling breakage, a hook
    traceback); an eval would not change the outcome.
    """

    PREVENTABLE = "preventable"
    ENVIRONMENTAL = "environmental"


_ENVIRONMENTAL_SLUG_FRAGMENTS: tuple[str, ...] = (
    "plugin-directory-does-not-exist",
    "hook-json-output-validation-failed",
    "failed-with-non-blocking-status-code",
    "failed-to-run",
    "non-blocking-error",
)
"""Gate-identity slug fragments whose failure is environmental, not agent-output-shaped.

A non-blocking hook error (a missing plugin directory, a hook runner traceback, a
hook-output validation breakage, or any other ``hook_non_blocking_error`` reduced
to the generic ``non-blocking-error`` identity) reflects the tooling/dependency
environment, not a piece of output the agent chose to write, so an eval cannot
make it pass first-try. A failure whose slug contains none of these fragments is
treated as preventable (fail toward an eval), including an unknown gate BLOCK.

Ordered most-specific-first so :func:`_infra_identity` is deterministic when a
single stderr matches more than one fragment: the concrete reason
(``plugin-directory-does-not-exist``) is a more dedup-meaningful identity than the
generic ``Failed to run:`` wrapper that prefixes it, and the bare
``non-blocking-error`` catch-all is last. A ``frozenset`` here made the chosen
identity depend on hash-seeded iteration order — the same stderr fingerprinted
two ways across processes, breaking recurrence dedup and making the command test
flaky. Order is the contract; ``classify_gate_failure`` is order-insensitive
(boolean ``any``), only ``_infra_identity`` reads the priority.
"""


def gate_identity_slug(marker_text: str) -> str:
    """Extract a minimal, bounded gate-identity slug from a gate's marker text.

    The teatree gate's own message leads with ``TEATREE GATE — <phrase>`` (or
    ``TEATREE LOOP SELF-PUMP — …``). This strips the marker prefix, keeps the
    first sentence, and reduces it to a short lowercase ``-``-joined slug of at
    most :data:`_SLUG_WORD_CAP` words and :data:`_SLUG_CHAR_CAP` chars. The slug
    is a stable identity token for the gate that is the same across sessions and
    tools while carrying none of the full message body. Non-marker text (an infra
    ``stderr``) is slugged the same way so an environmental failure still gets a
    bounded identity.
    """
    stripped = _MARKER_STRIP_RE.sub("", marker_text.strip())
    if _SELF_PUMP_MARKER.lower() in marker_text.lower():
        stripped = "self pump " + stripped
    first_sentence = _SENTENCE_SPLIT_RE.split(stripped, maxsplit=1)[0]
    words = _NON_SLUG_RE.sub(" ", first_sentence.lower()).split()
    slug = "-".join(words[:_SLUG_WORD_CAP])
    return slug[:_SLUG_CHAR_CAP].strip("-")


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One gate failure extracted from a session transcript.

    ``gate`` is the normalized identity slug (``<event>:<slug>``, e.g.
    ``stop:user-directed-question``). The record carries ONLY the slug, the hook
    event bucket, and the session id — never the raw blockingError message, the
    ``stderr``, the ``command``, or ``stdout`` (the leak vectors). The fingerprint
    hashes the slug so two firings of the same gate (any session, any tool) hash
    together while a different gate hashes apart.
    """

    gate: str
    hook_event: str
    session_id: str

    @property
    def fingerprint(self) -> str:
        """The dedup key: a stable hash of the gate-identity slug.

        Delegates to the :class:`~teatree.core.review.review_findings.ReviewFinding`
        adapter so the value recorded in the durable store and the value the
        escalation filer dedups on are ONE identical fingerprint — no second
        hashing scheme to drift.
        """
        return _as_finding(self).fingerprint

    def as_dict(self) -> RawAPIDict:
        """A privacy-safe serialization — gate-identity slug only, no message/stderr/command."""
        return {
            "gate": self.gate,
            "hook_event": self.hook_event,
            "session_id": self.session_id,
            "fingerprint": self.fingerprint,
        }


def extract_gate_failures(events: list[SessionEvent], *, session_id: str) -> list[GateFailure]:
    """Extract gate failures from the single hook-event chokepoint.

    Keys on the attachment TYPE, never on ``exitCode`` (a blocking gate carries
    none): a ``hook_blocking_error`` whose ``blockingError`` marker is a
    ``TEATREE GATE`` (NOT a ``TEATREE LOOP SELF-PUMP`` continue signal) is a
    preventable-shaped block; a ``hook_non_blocking_error`` is an infra failure.
    The gate identity is the marker phrase reduced to a bounded slug; the raw
    message / ``stderr`` / ``command`` are never copied onto the record.
    """
    failures: list[GateFailure] = []
    for event in extract_hook_events(events):
        attachment = event.raw.get("attachment")
        attachment = attachment if isinstance(attachment, dict) else {}
        gate = _gate_from_attachment(attachment, hook_event=event.hook_event or "")
        if gate is not None:
            failures.append(GateFailure(gate=gate, hook_event=event.hook_event or "", session_id=session_id))
    return failures


def _gate_from_attachment(attachment: RawAPIDict, *, hook_event: str) -> str | None:
    """Return the ``<event>:<slug>`` gate identity for a failure attachment, or ``None``.

    A blocking error carries a fixed teatree marker, so its first-sentence slug is
    a safe identity (it is the gate's OWN deterministic message, not agent output)
    — unless the marker is the self-pump continue-signal, which is not a failure.

    A non-blocking error's ``stderr`` is arbitrary and may carry sensitive content,
    so it is NEVER slugged verbatim: it is matched against the known environmental
    failure phrases (:func:`_infra_identity`) and reduced to the matched canonical
    slug, or the generic ``non-blocking-error`` identity. No arbitrary stderr ever
    enters the stored slug.
    """
    att_type = attachment.get("type")
    event_slug = _NON_SLUG_RE.sub("-", hook_event.lower()).strip("-")
    if att_type == _BLOCKING_ERROR_TYPE:
        marker = _blocking_error_message(attachment)
        if _SELF_PUMP_MARKER.lower() in marker.lower():
            return None
        return f"{event_slug}:{gate_identity_slug(marker)}"
    if att_type == _NON_BLOCKING_ERROR_TYPE:
        return f"{event_slug}:{_infra_identity(_str_field(attachment, 'stderr'))}"
    return None


def _infra_identity(stderr: str) -> str:
    """Map a non-blocking ``stderr`` to a canonical infra slug WITHOUT echoing it.

    The stderr is untrusted free text (a traceback, a path, possibly sensitive),
    so it is never slugged verbatim. It is matched against the known environmental
    failure phrases in :data:`_ENVIRONMENTAL_SLUG_FRAGMENTS` priority order (most
    specific reason first), so a stderr matching more than one fragment maps to a
    single deterministic identity; the matched canonical fragment is the identity,
    or the generic ``non-blocking-error`` when nothing matches. This keeps the
    stored identity bounded, content-free, and stable across processes.
    """
    lowered = _NON_SLUG_RE.sub("-", stderr.lower())
    for fragment in _ENVIRONMENTAL_SLUG_FRAGMENTS:
        if fragment in lowered:
            return fragment
    return "non-blocking-error"


def _blocking_error_message(attachment: RawAPIDict) -> str:
    """Read the ``attachment.blockingError.blockingError`` message text defensively."""
    blocking = attachment.get("blockingError")
    if isinstance(blocking, dict):
        inner = cast("RawAPIDict", blocking).get("blockingError")
        if isinstance(inner, str):
            return inner
    return _GATE_MARKER


def _str_field(attachment: RawAPIDict, key: str) -> str:
    value = attachment.get(key)
    return value if isinstance(value, str) else ""


def classify_gate_failure(failure: GateFailure) -> GateVerdict:
    """Classify a failure ``preventable`` / ``environmental`` via the declarative table.

    A failure whose identity slug contains an :data:`_ENVIRONMENTAL_SLUG_FRAGMENTS`
    fragment is environmental; everything else, including an unknown gate, is
    preventable — fail toward an eval.
    """
    if any(fragment in failure.gate for fragment in _ENVIRONMENTAL_SLUG_FRAGMENTS):
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
    reuses :func:`~teatree.core.review.review_findings.file_class_c_issue`, so it is
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

    The body is derived solely from the gate-identity slug (a bounded,
    banned-content-free token) so the ReviewFinding fingerprint is a stable
    function of the gate identity and matches :attr:`GateFailure.fingerprint`.
    """
    body = f"preventable gate `{failure.gate}` fired recurrently"
    return ReviewFinding(body=body, path="", line=0, author="gate-failure-loop")


def _enforcement_note(failure: GateFailure) -> str:
    return (
        f"preventable gate `{failure.gate}` fired recurrently. The smallest "
        "anti-vacuous eval to stop it first-try: a behavioral scenario whose "
        "prompt reproduces the violating output shape and whose matcher asserts "
        "the agent's output passes the gate (with a `_fail` fixture that goes RED)."
    )
