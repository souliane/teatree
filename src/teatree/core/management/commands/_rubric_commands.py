"""The ``ticket rubric-set`` / ``rubric-grade`` / ``rubric-show`` operator commands (#2241, #4832).

The three rubric commands live here as a :class:`RubricCommands` mixin that the
``ticket`` :class:`~django_typer.management.TyperCommand` inherits from, so they
mount under ``t3 <overlay> ticket rubric-set`` / ``rubric-grade`` / ``rubric-show`` while
their LOC stays out of the (already cap-bound) ``ticket.py`` god-module. django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO, so the
mixin is the idiomatic split — the CLI surface is unchanged.

``rubric-set`` takes EXPLICIT acceptance criteria (a JSON array of strings or
``{"text": ...}`` objects) beside the plan producer; ``rubric-grade``
records a verifier's per-criterion PASS/FAIL through the guarded
:meth:`RubricCriterion.record_grade` factory; ``rubric-show`` is the read seam a
reviewer needs to grade the checklist it was never handed inline (souliane/teatree#4832 —
a cold-reviewer sub-agent could verify every criterion but had no way to SEE the
rubric it must grade, short of an operator transcribing it by hand). The pure
parse/validate/mutate helpers raise :class:`RubricCommandError` (or
:class:`RubricError`) on a refusal, which the command translates to a ``stderr``
line + a nonzero exit.
"""

import json
from pathlib import Path
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.gates.rubric_gate import clear_honesty_escalation_on_pass
from teatree.core.models import Rubric, RubricError, Ticket
from teatree.core.models.types import RubricGrade


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


class RubricCriterionResult(TypedDict):
    ordinal: int
    text: str
    status: str
    grader_identity: str
    reviewed_sha: str
    rationale: str


class RubricShowResult(TypedDict, total=False):
    ticket_id: int
    rubric_id: int
    criteria: list[RubricCriterionResult]
    error: str


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


def parse_grades(grades_json: str) -> list[RubricGrade]:
    """The grade objects from ``--grades-json``, in the shared :class:`RubricGrade` shape.

    Raises :class:`RubricCommandError` on malformed JSON or a payload that is not a
    non-empty array — the CLI's own contract, which the envelope's does not share (an
    absent ``rubric_grades`` grades nothing rather than refusing). The per-ITEM shape is
    :meth:`Rubric.normalize_grades`, the one normaliser the reviewing recorder runs too,
    which raises :class:`RubricError`; both are refusals the command surfaces alike.
    """
    try:
        parsed = json.loads(grades_json) if grades_json.strip() else []
    except json.JSONDecodeError as exc:
        msg = f"--grades-json is not valid JSON ({exc})"
        raise RubricCommandError(msg) from exc
    if not isinstance(parsed, list) or not parsed:
        msg = "--grades-json must be a non-empty array of grade objects"
        raise RubricCommandError(msg)
    return Rubric.normalize_grades(parsed)


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
        resetting every grade to PENDING so a changed checklist is re-graded. This is
        the operator seam beside the plan producer, which ADDS and so never resets a
        grade. An empty / malformed / non-array payload is refused. Full contract:
        ``docs/blueprint/rubric-done-gate.md``.
        """
        ticket = self._resolve_rubric_ticket(ticket_id)
        try:
            criteria = parse_criteria(criteria_json, criteria_file)
            if criteria is None:
                self.stderr.write(
                    "  rubric-set refused: no criteria — pass --criteria-json or --criteria-file with a JSON array"
                )
                raise SystemExit(1)
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
            graded = rubric.apply_grades(grades, grader_identity=grader_identity, reviewed_sha=reviewed_sha)
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

    @command(name="rubric-show")
    def rubric_show(self, ticket_id: int) -> RubricShowResult:
        """Print a ticket's rubric — criteria, ordinals, grades, and the graded SHA (#4832).

        The read seam beside ``rubric-set`` / ``rubric-grade``: a reviewer that must GRADE
        a checklist could previously only verify it, with no command to see the criteria it
        was never briefed inline. Refuses when the ticket has no rubric, the same way
        ``rubric-grade`` does.
        """
        ticket = self._resolve_rubric_ticket(ticket_id)
        rubric = Rubric.objects.active_for_ticket(ticket)
        if rubric is None:
            self.stderr.write(f"  rubric-show refused: ticket {ticket.pk} has no rubric (set one with rubric-set)")
            raise SystemExit(1)
        criteria: list[RubricCriterionResult] = [
            {
                "ordinal": criterion.ordinal,
                "text": criterion.text,
                "status": criterion.status,
                "grader_identity": criterion.grader_identity,
                "reviewed_sha": criterion.reviewed_sha,
                "rationale": criterion.rationale,
            }
            for criterion in rubric.criteria.all()
        ]
        for entry in criteria:
            graded_sha = f" @{entry['reviewed_sha'][:8]}" if entry["reviewed_sha"] else ""
            self.stdout.write(
                f"  #{entry['ordinal']} [{entry['status']}]{graded_sha} {entry['text']}"
                + (f" — {entry['rationale']}" if entry["rationale"] else "")
            )
        return {"ticket_id": int(ticket.pk), "rubric_id": int(rubric.pk), "criteria": criteria}


__all__ = [
    "RubricCommandError",
    "RubricCommands",
    "RubricCriterionResult",
    "RubricGradeResult",
    "RubricSetResult",
    "RubricShowResult",
    "parse_criteria",
    "parse_grades",
    "set_rubric",
]
