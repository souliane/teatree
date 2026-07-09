r"""``t3 tokens`` — per-account Anthropic token health across configured accounts.

Top-level diagnostic over the per-account routing state
(``teatree.credential_config`` + the ``AnthropicTokenUsage`` health cache): it
enumerates every configured ``pass`` entry (the per-overlay OAuth + API-key lists
plus global) and reports each account's org id, unified 5h / weekly utilization,
weekly reset, and health status. A fresh cache row is reused with no network; a
stale/absent one triggers one live probe (an explicit report, so a refresh is
fine). The token that signs a probe is never rendered.

The structured value is the return (django-typer serialises it) — JSON when
``--json``, else the human table.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand

_ADHOC_HELP = (
    "Ad-hoc Anthropic token to health-probe as an extra row (repeatable) — for checking a "
    "freshly-minted token before saving it. Warning: a token on the command line is visible "
    "in 'ps' output and your shell history."
)


class Command(TyperCommand):
    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human table."),
        ] = False,
        tokens: Annotated[list[str] | None, typer.Option("--token", help=_ADHOC_HELP)] = None,
    ) -> str:
        """Show per-account Anthropic 5h / weekly token utilization + status."""
        from teatree.token_report import TokenReport, render_table  # noqa: PLC0415

        rows = TokenReport(ad_hoc_tokens=tokens).rows()
        if json_output:
            return json.dumps([row.as_dict() for row in rows])
        return render_table(rows)
