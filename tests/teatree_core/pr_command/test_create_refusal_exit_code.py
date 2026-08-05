"""``pr create`` refusals exit non-zero on the argv path (#4210).

#4206 replaced a host-side ``OperationalError`` traceback with a named refusal
and, in doing so, turned exit 1 into exit 0 — so ``t3 <overlay> ship <id> &&
t3 <overlay> ticket clear …`` ran the second command having shipped nothing.
The message is load-bearing and unchanged; only the status moves.

The control-DB refusal is read before any ORM touch by design (#4170), so these
drive the real command with no database.
"""

from pathlib import Path
from typing import cast, get_args, get_type_hints
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.management.commands import _pr_control_db, _pr_ticket_resolve
from teatree.core.management.commands.pr import Command
from teatree.core.management.refusal_exit import REFUSAL_EXIT_CODE, RefusalExitTyperCommand
from teatree.core.models import Ticket
from teatree.paths import CONTROL_DB_DIR_ENV, DB_FILENAME

from ._shared import _MOCK_OVERLAY

_CONTAINER_ONLY = Path("/nonexistent/container-only/control-db")

#: The result shapes ``create`` returns on a path that DID ship (or previewed) —
#: everything else in its return union is a refusal and must carry ``error``.
_SUCCESS_SHAPES = frozenset({"ShipEnqueued", "ShipExecuted", "ShipDryRun"})


@pytest.fixture
def _container_only_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONTROL_DB_DIR_ENV, str(_CONTAINER_ONLY))
    monkeypatch.setattr(_pr_control_db, "configured_db_path", lambda: _CONTAINER_ONLY / DB_FILENAME)


@pytest.mark.usefixtures("_container_only_db")
class TestHostSidePrCreateRefusalExitsNonZero:
    def test_the_argv_path_exits_non_zero(self) -> None:
        resolve = MagicMock(side_effect=Ticket.DoesNotExist)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_ticket_resolve, "resolve_ticket", resolve),
            pytest.raises(SystemExit) as exc,
        ):
            Command().run_from_argv(["manage.py", "pr", "create", "4242"])

        assert exc.value.code == REFUSAL_EXIT_CODE
        resolve.assert_not_called()

    def test_the_message_and_hint_are_unchanged_for_an_in_process_caller(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", "4242"))

        assert str(_CONTAINER_ONLY) in str(result["error"])
        assert "deploy/t3" in str(result["hint"])


class TestEveryRefusalCreateCanReturnIsKeyedOnError:
    """The seam keys on ``error``; a refusal shape lacking it would exit 0 again."""

    def test_the_command_carries_the_seam(self) -> None:
        assert issubclass(Command, RefusalExitTyperCommand)

    def test_each_non_success_return_shape_declares_an_error_key(self) -> None:
        members = get_args(get_type_hints(Command.create)["return"])
        refusals = [shape for shape in members if shape.__name__ not in _SUCCESS_SHAPES]

        assert refusals, "the return union lost every refusal shape — the pin is vacuous"
        for shape in refusals:
            assert "error" in get_type_hints(shape), f"{shape.__name__} would exit 0 on a refusal"
