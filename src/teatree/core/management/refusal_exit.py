"""A returned structured refusal becomes a non-zero process exit (#4210).

A command that RETURNS ``{"error": …}`` instead of raising hands an in-process
caller a value to route on — the shape ``mcp`` and the loop read. But Django's
``run_from_argv`` discards the return, so the process exits 0: ``t3 <overlay>
ship <id> && t3 <overlay> ticket clear …`` runs the second command having
shipped nothing, and a ``set -e`` lane proceeds on a refusal. That is the
anti-pattern named in ``/t3:internals`` § Management Command Patterns.

The seam is the boundary, not the call sites: one base class maps a returned
refusal to a non-zero exit on the argv path alone, so the structured value still
reaches in-process callers unchanged and only the shell sees a failure.
"""

from collections.abc import Mapping
from typing import cast

from django_typer.management import TyperCommand

#: The key a refusal is recognised by — the convention every ``error``-shaped
#: result ``TypedDict`` under ``commands/`` already follows.
REFUSAL_KEY = "error"

REFUSAL_EXIT_CODE = 1


def refusal_exit_code(result: object) -> int:
    """The exit code *result* implies — non-zero iff it carries a non-empty refusal."""
    if not isinstance(result, Mapping):
        return 0
    refusal = cast("Mapping[str, object]", result)
    return REFUSAL_EXIT_CODE if refusal.get(REFUSAL_KEY) else 0


class RefusalExitTyperCommand(TyperCommand):
    """A ``TyperCommand`` whose returned refusal exits non-zero when run from argv.

    Gated on ``_called_from_command_line`` — the flag Django's ``run_from_argv``
    sets and ``call_command`` does not — because the two callers need opposite
    things from one refusal: a shell needs a failing status, an in-process
    consumer needs the dict.
    """

    def execute(self, *args: object, **options: object) -> object:
        result = super().execute(*args, **options)
        code = refusal_exit_code(result)
        if code and self._called_from_command_line:
            raise SystemExit(code)
        return result
