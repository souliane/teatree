"""The ``ticket record-spec-coverage`` operator command — the #2232 manifest WRITER.

The spec-coverage DoD gate (:mod:`teatree.core.gates.spec_coverage_gate`) shipped
read-only: it treats a MISSING ``ticket.extra['spec_coverage']`` as a hard block,
and nothing wrote one. Turning ``require_spec_coverage`` on therefore refused
every RETROSPECTED→DELIVERED advance — the ON state was "delivery bricked", not
"coverage enforced". This command is the producer that makes the flag
satisfiable, mirroring ``lifecycle record-anti-vacuity`` / ``repro record-red``.

It lives here as a :class:`SpecCoverageCommands` mixin the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits, so it mounts under
``t3 <overlay> ticket record-spec-coverage`` while its LOC stays out of the
cap-bound ``ticket.py`` (same split as :mod:`._rubric_commands`).
"""

from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.gates.spec_coverage_gate import acceptance_criteria, uncovered_acs
from teatree.core.models import Ticket
from teatree.core.models.types import AcceptanceCriterion


class SpecCoverageResult(TypedDict, total=False):
    ticket_id: int
    acceptance_criteria: int
    uncovered: list[str]
    override_reason: str


def parse_ac_specs(specs: list[str]) -> list[AcceptanceCriterion]:
    """Parse repeatable ``--ac '<label>=<test>[,<test>…]'`` flags into manifest entries.

    A bare ``'<label>='`` is deliberately legal: it records a DECLARED but
    uncovered AC, which the gate then blocks on — an honest manifest names the
    ACs it cannot yet prove instead of omitting them.
    """
    criteria: list[AcceptanceCriterion] = []
    for spec in specs:
        label, separator, tests = spec.partition("=")
        if not separator or not label.strip():
            msg = f"--ac {spec!r} must be '<label>=<test>[,<test>…]'"
            raise ValueError(msg)
        criteria.append({"id": label.strip(), "tests": [test.strip() for test in tests.split(",") if test.strip()]})
    return criteria


class SpecCoverageCommands(TyperCommand):
    """The ``ticket record-spec-coverage`` command, mounted via MRO inheritance."""

    def _resolve_spec_coverage_ticket(self, ticket_id: int) -> Ticket:
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  refused: ticket {ticket_id} not found")
            raise SystemExit(1) from None

    @command(name="record-spec-coverage")
    def record_spec_coverage(
        self,
        ticket_id: int,
        *,
        ac: Annotated[
            list[str] | None,
            typer.Option(
                "--ac",
                help="'<label>=<test>[,<test>…]' — one acceptance criterion and its backing tests. Repeatable.",
            ),
        ] = None,
        replace: Annotated[
            bool,
            typer.Option(help="Record exactly these ACs instead of upserting them into the existing manifest."),
        ] = False,
        override_reason: Annotated[
            str,
            typer.Option("--override-reason", help="Audited escape hatch: why this ticket genuinely has no ACs."),
        ] = "",
    ) -> SpecCoverageResult:
        """Record the spec-coverage manifest the delivery DoD gate reads (#2232).

        Each ``--ac`` maps one acceptance criterion to the test(s) that prove it,
        so ``require_spec_coverage`` can refuse a RETROSPECTED→DELIVERED advance
        that would declare done on a partial subset of the spec.

        ACs are upserted by label; ``--replace`` records exactly the given set.
        An AC recorded with no tests (``--ac 'AC3='``) is declared-but-uncovered
        and still blocks. ``--override-reason`` records the audited exemption for
        a genuinely AC-less ticket (a pure refactor, a docs-only change).
        """
        ticket = self._resolve_spec_coverage_ticket(ticket_id)
        reason = override_reason.strip()
        try:
            criteria = parse_ac_specs(ac or [])
        except ValueError as exc:
            self.stderr.write(f"  record-spec-coverage refused: {exc}")
            raise SystemExit(1) from None
        if not criteria and not reason:
            self.stderr.write(
                "  record-spec-coverage refused: pass at least one --ac, or --override-reason for an AC-less ticket."
            )
            raise SystemExit(1)
        if criteria:
            ticket.record_spec_coverage(criteria, replace=replace)
        if reason:
            ticket.record_spec_coverage_override(reason)
        uncovered = uncovered_acs(ticket)
        recorded = len(acceptance_criteria(ticket))
        self.stdout.write(
            f"  recorded spec coverage for ticket {ticket.pk}: {recorded} AC(s), {len(uncovered)} uncovered"
        )
        return SpecCoverageResult(
            ticket_id=int(ticket.pk),
            acceptance_criteria=recorded,
            uncovered=uncovered,
            override_reason=reason,
        )
