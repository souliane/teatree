r"""``t3 tokens`` — per-account Anthropic token health across configured accounts.

Top-level diagnostic over the per-account routing state
(``teatree.credential_config`` + the ``AnthropicTokenUsage`` health cache): it
enumerates every configured ``pass`` entry (the per-overlay OAuth + API-key lists
plus global) and reports each account's org id, unified 5h / weekly utilization,
the 5h next-window reset and weekly reset, and health status. A fresh cache row is
reused with no network; a
stale/absent one triggers one live probe (an explicit report, so a refresh is
fine). The token that signs a probe is never rendered.

The rows are routed through the machine-output seam — JSON on stdout under
``--json``, the human table on stderr — and returned as the typed payload.
"""

from typing import IO, Annotated, cast

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.token_report import TokenAccountPayload

_ADHOC_HELP = (
    "Ad-hoc Anthropic token to health-probe as an extra row (repeatable) — for checking a "
    "freshly-minted token before saving it. Warning: a token on the command line is visible "
    "in 'ps' output and your shell history."
)


class Command(MachineOutputCommand):
    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human table."),
        ] = False,
        tokens: Annotated[list[str] | None, typer.Option("--token", help=_ADHOC_HELP)] = None,
    ) -> list[TokenAccountPayload]:
        """Show per-account Anthropic 5h / weekly token utilization + status."""
        from teatree.token_report import TokenReport, render_table  # noqa: PLC0415 — deferred: lazy command import

        rows = TokenReport(ad_hoc_tokens=tokens).rows()
        payload = [row.as_dict() for row in rows]
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=render_table(rows),
        )
        return payload
