"""Ratchet: no management command may bypass the machine-output seam.

``t3`` is a machine interface — a front-end shells to ``t3 ... --json`` and parses
stdout, so stdout must be a PURE data channel. :mod:`teatree.core.machine_output`
is the contract and ``emit`` its one seam;
:mod:`teatree.quality.machine_output_seam` is the static detector.

The seam rollout is incomplete, so this is a shrink-only ratchet rather than a
zero-assertion. :data:`UNCONVERTED` names every handler still awaiting
conversion; the assertion is set equality, so BOTH directions fail on purpose:

A NEW handler that returns a typed payload without ``print_result = False``, or
declares ``--json`` without routing through ``emit``, is not in the ledger and
turns this red — the regression class this gate exists to foreclose. A CONVERTED
handler still listed in the ledger also turns it red, so every conversion removes
its line in the same commit and the ledger can only shrink.

Nothing may be added to :data:`UNCONVERTED` or :data:`UNPINNED_SCALAR_RETURNS`. A new
command routes through ``emit`` from the start, and pins its typed return.
"""

# test-path: cross-cutting — a whole-tree quality gate over every management command.
from pathlib import Path

import pytest

from teatree.quality.machine_output_seam import DefectKind, SeamDefect, scan_defects

_COMMANDS_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core" / "management" / "commands"

# Handlers returning a structured payload with no ``print_result = False``, so
# django-typer additionally ``str()``-es the return onto stdout as Python repr.
# Shrink-only: delete a line when its handler is converted; never add one.
UNCONVERTED: frozenset[str] = frozenset(
    {
        "_attachment_commands:AttachmentCommands.attachments:typed-return-unpinned",
        "_close_commands:CloseCommands.bulk_close:typed-return-unpinned",
        "_close_commands:CloseCommands.integration_review_override:typed-return-unpinned",
        "_context_commands:ContextCommands.context_add:typed-return-unpinned",
        "_context_commands:ContextCommands.context_edit:typed-return-unpinned",
        "_context_commands:ContextCommands.context_show:typed-return-unpinned",
        "_merge_keystone_commands:MergeKeystoneCommands.merge:typed-return-unpinned",
        "_plan_commands:PlanCommands.plan:typed-return-unpinned",
        "_plan_commands:PlanCommands.plan_bypass:typed-return-unpinned",
        "_plan_commands:PlanCommands.plan_reaffirm:typed-return-unpinned",
        "_plan_commands:PlanCommands.plan_reconcile_inflight:typed-return-unpinned",
        "_plan_commands:PlanCommands.skip_planning:typed-return-unpinned",
        "_rubric_commands:RubricCommands.rubric_grade:typed-return-unpinned",
        "_rubric_commands:RubricCommands.rubric_set:typed-return-unpinned",
        "_spec_coverage_commands:SpecCoverageCommands.record_spec_coverage:typed-return-unpinned",
        "_sweep_commands:SweepCommands.reconcile_overlay:typed-return-unpinned",
        "_sweep_commands:SweepCommands.sync_completions:typed-return-unpinned",
        "_ticket_show:TicketShowCommands.expedite:typed-return-unpinned",
        "_ticket_show:TicketShowCommands.show:typed-return-unpinned",
        "e2e:Command.post_evidence:typed-return-unpinned",
        "e2e:Command.post_test_plan:typed-return-unpinned",
        "e2e:Command.trigger_ci:typed-return-unpinned",
        "followup:Command.discover_mrs:typed-return-unpinned",
        "followup:Command.refresh:typed-return-unpinned",
        "followup:Command.remind:typed-return-unpinned",
        "identities:Command.add:typed-return-unpinned",
        "identities:Command.list_:typed-return-unpinned",
        "identities:Command.remove:typed-return-unpinned",
        "identities:Command.seed:typed-return-unpinned",
        "learnings:Command.add:typed-return-unpinned",
        "learnings:Command.edit:typed-return-unpinned",
        "learnings:Command.show:typed-return-unpinned",
        "lifecycle:Command.record_e2e_run:typed-return-unpinned",
        "mr_reminder:Command.preview:typed-return-unpinned",
        "mr_reminder:Command.send:typed-return-unpinned",
        "pr:Command.check_gates:typed-return-unpinned",
        "pr:Command.create:typed-return-unpinned",
        "pr:Command.ensure_pr:typed-return-unpinned",
        "pr:Command.fetch_issue:typed-return-unpinned",
        "pr:Command.merge:typed-return-unpinned",
        "pr:Command.post_evidence:typed-return-unpinned",
        "pr:Command.post_test_plan:typed-return-unpinned",
        "pr:Command.sweep:typed-return-unpinned",
        "repro:Command.record_green:typed-return-unpinned",
        "repro:Command.record_red:typed-return-unpinned",
        "repro:Command.waive:typed-return-unpinned",
        "review:Command.lock_acquire:typed-return-unpinned",
        "review:Command.lock_status:typed-return-unpinned",
        "review:Command.rebind_clearance:typed-return-unpinned",
        "review_request_check:Command.handle:typed-return-unpinned",
        "run:Command.services:typed-return-unpinned",
        "run:Command.verify:typed-return-unpinned",
        "standup:Command.generate:typed-return-unpinned",
        "standup:Command.stale:typed-return-unpinned",
        "tasks:Command.work_next:typed-return-unpinned",
        "ticket:Command.clear:typed-return-unpinned",
        "ticket:Command.comment:typed-return-unpinned",
        "ticket:Command.create_sub:typed-return-unpinned",
        "ticket:Command.dod_override:typed-return-unpinned",
        "ticket:Command.e2e_bypass:typed-return-unpinned",
        "ticket:Command.list_tickets:typed-return-unpinned",
        "ticket:Command.transition:typed-return-unpinned",
        "workspace:Command.clean_all:typed-return-unpinned",
        "workspace:Command.clean_merged:typed-return-unpinned",
        "workspace:Command.doctor:typed-return-unpinned",
        "workspace:Command.landscape:typed-return-unpinned",
        "workspace:Command.list_orphans:typed-return-unpinned",
        "workspace:Command.reap_stale:typed-return-unpinned",
        "workspace:Command.relocate:typed-return-unpinned",
        "workspace:Command.stamp_identity:typed-return-unpinned",
        "worktree:Command.smoke_test:typed-return-unpinned",
    }
)

# Handlers returning a bare non-``str`` scalar with no ``print_result = False``, so a
# caller that captures their output is handed the raw value to ``.endswith``
# (souliane/teatree#4467). Shrink-only, same contract as UNCONVERTED above.
UNPINNED_SCALAR_RETURNS: frozenset[str] = frozenset(
    {
        "env:Command.check_drift:non-str-scalar-return-unpinned",
        "env:Command.migrate_secrets:non-str-scalar-return-unpinned",
        "env:Command.overrides:non-str-scalar-return-unpinned",
        "env:Command.set_var:non-str-scalar-return-unpinned",
        "env:Command.show:non-str-scalar-return-unpinned",
        "env:Command.unset:non-str-scalar-return-unpinned",
        "tasks:Command.claim:non-str-scalar-return-unpinned",
        "worktree:Command.provision:non-str-scalar-return-unpinned",
    }
)

LEDGER: frozenset[str] = UNCONVERTED | UNPINNED_SCALAR_RETURNS


@pytest.fixture(scope="module")
def defects() -> tuple[SeamDefect, ...]:
    return tuple(scan_defects(_COMMANDS_DIR))


class TestSeamRatchet:
    def test_no_unlisted_bypass(self, defects: tuple[SeamDefect, ...]) -> None:
        unlisted = sorted(d.key for d in defects if d.key not in LEDGER)
        assert not unlisted, (
            "management command handler(s) bypass the machine-output seam:\n"
            + "\n".join(f"  {key}" for key in unlisted)
            + "\n\nRoute the payload through `teatree.core.machine_output.emit` and set "
            "`self.print_result = False` (see `do.py` / `retention.py` for the pattern). "
            "Do NOT add the handler to UNCONVERTED — that ledger is shrink-only."
        )

    def test_ledger_carries_no_converted_handler(self, defects: tuple[SeamDefect, ...]) -> None:
        live = {d.key for d in defects}
        stale = sorted(LEDGER - live)
        assert not stale, (
            "The ledger lists handler(s) that no longer bypass the seam:\n"
            + "\n".join(f"  {key}" for key in stale)
            + "\n\nDelete each line in the same commit as its conversion."
        )

    def test_ledger_holds_only_the_unpinned_return_classes(self) -> None:
        """Every ``--json`` handler is converted, so only the unpinned-return classes remain."""
        kinds = {key.rsplit(":", 1)[-1] for key in LEDGER}
        assert kinds == {
            DefectKind.TYPED_RETURN_UNPINNED.value,
            DefectKind.NON_STR_SCALAR_RETURN_UNPINNED.value,
        }


class TestDetectorBites:
    """Anti-vacuity: the detector must go red on a planted violation."""

    @staticmethod
    def _write(tmp_path: Path, body: str) -> Path:
        (tmp_path / "planted.py").write_text(body)
        return tmp_path

    def test_typed_return_without_pin_is_a_defect(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path,
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self) -> dict[str, int]:\n"
            "        return {'a': 1}\n",
        )
        assert [d.kind for d in scan_defects(root)] == [DefectKind.TYPED_RETURN_UNPINNED]

    def test_non_str_scalar_return_without_pin_is_a_defect(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path,
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self) -> int:\n"
            "        return 1091\n",
        )
        assert [d.kind for d in scan_defects(root)] == [DefectKind.NON_STR_SCALAR_RETURN_UNPINNED]

    def test_a_pinned_scalar_return_is_clean(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path,
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self) -> int:\n"
            "        self.print_result = False\n"
            "        return 1091\n",
        )
        assert scan_defects(root) == []

    def test_a_bare_str_return_stays_exempt(self, tmp_path: Path) -> None:
        """Both wrappers write a ``str`` through unchanged — there is nothing to crash."""
        root = self._write(
            tmp_path,
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self) -> str:\n"
            "        return 'ok'\n",
        )
        assert scan_defects(root) == []

    def test_json_flag_without_emit_is_a_defect(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path,
            "from typing import Annotated\n"
            "import typer\n"
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self, *, json_output: Annotated[bool, typer.Option('--json')] = False) -> None:\n"
            "        self.stdout.write('{}')\n",
        )
        assert [d.kind for d in scan_defects(root)] == [DefectKind.JSON_FLAG_BYPASSES_SEAM]

    def test_converted_handler_is_clean(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path,
            "from typing import Annotated\n"
            "import typer\n"
            "from django_typer.management import TyperCommand\n"
            "from teatree.core.machine_output import emit\n"
            "class Command(TyperCommand):\n"
            "    def handle(self, *, json_output: Annotated[bool, typer.Option('--json')] = False)"
            " -> dict[str, int]:\n"
            "        payload = {'a': 1}\n"
            "        self.print_result = False\n"
            "        emit(payload, json_output=json_output, out=self.stdout, err=self.stderr)\n"
            "        return payload\n",
        )
        assert scan_defects(root) == []

    def test_module_local_delegation_counts_as_routed(self, tmp_path: Path) -> None:
        """The ``loop_state`` shape: the handler's whole body is a helper that emits."""
        root = self._write(
            tmp_path,
            "from typing import Annotated\n"
            "import typer\n"
            "from django_typer.management import TyperCommand\n"
            "from teatree.core.machine_output import emit\n"
            "def _report(command, *, json_output):\n"
            "    emit({}, json_output=json_output, out=command.stdout, err=command.stderr)\n"
            "class Command(TyperCommand):\n"
            "    def handle(self, *, json_output: Annotated[bool, typer.Option('--json')] = False) -> None:\n"
            "        _report(self, json_output=json_output)\n",
        )
        assert scan_defects(root) == []

    def test_sibling_module_delegation_counts_as_routed(self, tmp_path: Path) -> None:
        """The ``e2e lanes`` shape: the verb's body lives in an aliased sibling module."""
        (tmp_path / "_helper.py").write_text(
            "from teatree.core.machine_output import emit\n"
            "def run_verb(*, as_json, out, err):\n"
            "    emit({}, json_output=as_json, out=out, err=err)\n"
        )
        root = self._write(
            tmp_path,
            "from typing import Annotated\n"
            "import typer\n"
            "from django_typer.management import TyperCommand\n"
            "from teatree.core.management.commands import _helper as _h\n"
            "class Command(TyperCommand):\n"
            "    def handle(self, *, json_output: Annotated[bool, typer.Option('--json')] = False) -> None:\n"
            "        _h.run_verb(as_json=json_output, out=self.stdout, err=self.stderr)\n",
        )
        assert scan_defects(root) == []

    def test_name_collision_with_a_sibling_helper_does_not_absolve(self, tmp_path: Path) -> None:
        """A bare ``self.run_verb()`` must NOT resolve to an unrelated sibling of the same name."""
        (tmp_path / "_helper.py").write_text(
            "from teatree.core.machine_output import emit\n"
            "def run_verb(*, out, err):\n"
            "    emit({}, json_output=True, out=out, err=err)\n"
        )
        root = self._write(
            tmp_path,
            "from typing import Annotated\n"
            "import typer\n"
            "from django_typer.management import TyperCommand\n"
            "class Command(TyperCommand):\n"
            "    def handle(self, *, json_output: Annotated[bool, typer.Option('--json')] = False) -> None:\n"
            "        self.run_verb()\n",
        )
        assert [d.kind for d in scan_defects(root)] == [DefectKind.JSON_FLAG_BYPASSES_SEAM]
