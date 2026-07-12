"""The ``ticket rubric-set`` / ``rubric-grade`` operator commands, factored out of ``ticket.py`` (#2241).

The two rubric commands live here as a :class:`RubricCommands` mixin that the
``ticket`` :class:`~django_typer.management.TyperCommand` inherits from, so they
mount under ``t3 <overlay> ticket rubric-set`` / ``rubric-grade`` while their LOC
stays out of the (already cap-bound) ``ticket.py`` god-module. django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO, so the
mixin is the idiomatic split — the CLI surface is unchanged.

``rubric-set`` takes EXPLICIT acceptance criteria (a JSON array of strings or
``{"text": ...}`` objects — no ``/plan`` derivation, that is [#2240]); ``rubric-grade``
records a verifier's per-criterion PASS/FAIL through the guarded
:meth:`RubricCriterion.record_grade` factory. The pure parse/validate/mutate
helpers raise :class:`RubricCommandError` (or :class:`RubricError`) on a refusal,
which the command translates to a ``stderr`` line + a nonzero exit.
"""

import json
from pathlib import Path
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Rubric, RubricCriterion, RubricError, Ticket


class RubricCommandError(ValueError):
    """A rubric command's input was rejected — the command surfaces it as a refusal."""


class RubricSetResult(TypedDict, total=False):
    ticket_id: int
    rubric_id: int
    criteria_count: int
    error: str


class RubricGradeResult(TypedDict, total=False):
    ticket_id: int
    rubric_id: int
    graded_count: int
    fully_passed: bool
    error: str


class GradeInput(TypedDict, total=False):
    ordinal: object
    status: object
    rationale: object


def parse_criteria(criteria_json: str, criteria_file: str) -> list[str] | None:
    """The criterion texts from ``--criteria-json`` / ``--criteria-file``, or ``None``.

    Accepts a JSON array of strings (``["AC1"]``) or of objects carrying a ``text``
    key (``[{"text": "AC1"}]``). Returns ``None`` when neither source is given;
    raises :class:`RubricCommandError` on malformed JSON or a non-array /
    wrong-shaped payload (a silent mis-parse must not produce an empty rubric).
    """
    raw = Path(criteria_file).read_text(encoding="utf-8") if criteria_file else criteria_json
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"criteria is not valid JSON ({exc})"
        raise RubricCommandError(msg) from exc
    if not isinstance(parsed, list):
        msg = "criteria JSON must be an array of strings or {text} objects"
        raise RubricCommandError(msg)
    texts: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])
        else:
            msg = f"each criterion must be a string or a {{text}} object: {item!r}"
            raise RubricCommandError(msg)
    return texts


def set_rubric(ticket: Ticket, criteria: list[str]) -> Rubric:
    """Populate the ticket's rubric with the explicit ``criteria`` (all PENDING).

    Thin wrapper over :meth:`Rubric.populate` so the command stays a delegator;
    :class:`RubricError` (empty list) propagates to the command's refusal path.
    """
    return Rubric.populate(ticket, criteria)


def parse_grades(grades_json: str) -> list[GradeInput]:
    """The grade objects from ``--grades-json``, validated to be a non-empty array.

    Raises :class:`RubricCommandError` on malformed JSON, a non-array payload, an
    empty array, or any item that is not an object carrying ``ordinal`` + ``status``.
    """
    try:
        parsed = json.loads(grades_json) if grades_json.strip() else []
    except json.JSONDecodeError as exc:
        msg = f"--grades-json is not valid JSON ({exc})"
        raise RubricCommandError(msg) from exc
    if not isinstance(parsed, list) or not parsed:
        msg = "--grades-json must be a non-empty array of grade objects"
        raise RubricCommandError(msg)
    grades: list[GradeInput] = []
    for item in parsed:
        if not isinstance(item, dict):
            msg = f"each grade must be an object: {item!r}"
            raise RubricCommandError(msg)
        fields = {str(key): value for key, value in item.items()}
        if fields.get("ordinal") is None or fields.get("status") is None:
            msg = f"each grade needs an ordinal and a status: {item!r}"
            raise RubricCommandError(msg)
        grades.append(
            GradeInput(ordinal=fields["ordinal"], status=fields["status"], rationale=fields.get("rationale", ""))
        )
    return grades


def clear_honesty_escalation_on_pass(ticket: Ticket) -> None:
    """Clear the ticket's active honesty escalations on a verified-complete landing (#2263).

    The PRIMARY clear for a :class:`~teatree.core.models.honesty_escalation.HonestyEscalation`:
    when ``rubric-grade`` records a fully-passed rubric, the ticket landed an
    honest, verified-complete outcome, so any active escalation for the ticket's
    sessions is cleared (the TTL is only the safety-net backstop). Keyed to the
    ticket's session ``agent_id``s. Fail-SAFE: a recording error never blocks the
    grade command (the grade is already recorded — this is post-success cleanup).
    """
    from teatree.core.models.honesty_escalation import HonestyEscalation  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        sessions = ticket.sessions.exclude(agent_id="")  # ty: ignore[unresolved-attribute]
        for agent_id in sessions.values_list("agent_id", flat=True).distinct():
            HonestyEscalation.mark_cleared(agent_id)
    except Exception:  # noqa: BLE001 — best-effort side-effect; a failure degrades to no-op
        return


def apply_grades(rubric: Rubric, grades: list[GradeInput], *, grader_identity: str, reviewed_sha: str) -> int:
    """Stamp each grade through the guarded factory; raise on the first refusal.

    An unknown ordinal raises :class:`RubricCommandError`; an invalid grade (maker
    grader / bad SHA / bad status) raises :class:`RubricError`. Either aborts the
    grading — a partial run that leaves some criteria silently ungraded must not
    read as success. Returns the number graded.
    """
    graded = 0
    for grade in grades:
        ordinal = grade["ordinal"]
        try:
            criterion = rubric.criteria.get(ordinal=ordinal)
        except RubricCriterion.DoesNotExist as exc:
            msg = f"no criterion with ordinal {ordinal!r}"
            raise RubricCommandError(msg) from exc
        criterion.record_grade(
            status=str(grade["status"]),
            grader_identity=grader_identity,
            reviewed_sha=reviewed_sha,
            rationale=str(grade.get("rationale", "")),
        )
        graded += 1
    return graded


class RubricCommands(TyperCommand):
    """The ``ticket rubric-set`` / ``rubric-grade`` commands, mounted via MRO inheritance.

    The ``ticket`` :class:`~django_typer.management.TyperCommand` inherits this mixin
    so the two commands mount under it while their bodies live here, off the cap-bound
    ``ticket.py``. Both resolve the ticket, run the pure parse/validate/mutate helpers,
    and translate a :class:`RubricCommandError` / :class:`RubricError` into a ``stderr``
    refusal + a nonzero exit.
    """

    def _resolve_rubric_ticket(self, ticket_id: int) -> Ticket:
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  refused: ticket {ticket_id} not found")
            raise SystemExit(1) from None

    @command(name="rubric-set")
    def rubric_set(
        self,
        ticket_id: int,
        *,
        criteria_json: Annotated[
            str,
            typer.Option("--criteria-json", help='JSON array: \'["AC1"]\' or \'[{"text": "AC1"}]\'.'),
        ] = "",
        criteria_file: Annotated[
            str,
            typer.Option("--criteria-file", help="Path to a JSON criteria-array file."),
        ] = "",
    ) -> RubricSetResult:
        """Set a ticket's rubric from EXPLICIT JSON criteria, all PENDING (#2241).

        Replaces the ticket's :class:`Rubric` criteria atomically (a get-or-create),
        resetting every grade to PENDING so a changed checklist is re-graded. The
        criteria are explicit — auto-derivation from ``/plan`` is the [#2240] follow-up.
        An empty / malformed / non-array payload is refused. Full contract:
        ``docs/blueprint/rubric-done-gate.md``.
        """
        ticket = self._resolve_rubric_ticket(ticket_id)
        try:
            criteria = parse_criteria(criteria_json, criteria_file)
            if criteria is None:
                return {"error": "No criteria: pass --criteria-json or --criteria-file with a JSON array"}
            rubric = set_rubric(ticket, criteria)
        except (RubricCommandError, RubricError) as exc:
            self.stderr.write(f"  rubric-set refused: {exc}")
            raise SystemExit(1) from exc
        count = rubric.criteria.count()
        self.stdout.write(f"  set rubric {rubric.pk} for ticket {ticket.pk} with {count} criteria")
        return {"ticket_id": int(ticket.pk), "rubric_id": int(rubric.pk), "criteria_count": count}

    @command(name="rubric-grade")
    def rubric_grade(
        self,
        ticket_id: int,
        *,
        grades_json: Annotated[
            str,
            typer.Option("--grades-json", help='JSON: \'[{"ordinal": 0, "status": "pass"}]\'.'),
        ] = "",
        grader_identity: Annotated[
            str,
            typer.Option("--grader-identity", help="Independent verifier id (NOT a maker/coding-agent/loop role)."),
        ] = "",
        reviewed_sha: Annotated[
            str,
            typer.Option("--reviewed-sha", help="Full 40-char hex SHA of the graded tree (the verifier's head)."),
        ] = "",
    ) -> RubricGradeResult:
        """Record a verifier's per-criterion PASS/FAIL on a ticket's rubric (#2241).

        Each grade is stamped through the guarded :meth:`RubricCriterion.record_grade`
        factory (grader != maker, terminal status, 40-char-hex SHA); criteria not named
        stay PENDING (fail-closed). The rubric is fully passed only when EVERY criterion
        is PASS by this independent grader at the head SHA. Full contract:
        ``docs/blueprint/rubric-done-gate.md``.
        """
        ticket = self._resolve_rubric_ticket(ticket_id)
        rubric = Rubric.objects.active_for_ticket(ticket)
        if rubric is None:
            self.stderr.write(f"  rubric-grade refused: ticket {ticket.pk} has no rubric (set one with rubric-set)")
            raise SystemExit(1)
        try:
            grades = parse_grades(grades_json)
            graded = apply_grades(rubric, grades, grader_identity=grader_identity, reviewed_sha=reviewed_sha)
        except (RubricCommandError, RubricError) as exc:
            self.stderr.write(f"  rubric-grade refused: {exc}")
            raise SystemExit(1) from exc
        fully_passed = rubric.is_fully_passed_at(reviewed_sha)
        if fully_passed:
            clear_honesty_escalation_on_pass(ticket)
        self.stdout.write(f"  graded {graded} criteria on rubric {rubric.pk} (fully passed: {fully_passed})")
        return {
            "ticket_id": int(ticket.pk),
            "rubric_id": int(rubric.pk),
            "graded_count": graded,
            "fully_passed": fully_passed,
        }


__all__ = [
    "GradeInput",
    "RubricCommandError",
    "RubricCommands",
    "RubricGradeResult",
    "RubricSetResult",
    "apply_grades",
    "clear_honesty_escalation_on_pass",
    "parse_criteria",
    "parse_grades",
    "set_rubric",
]
