r"""``t3 tokens`` — per-account Anthropic token health across configured accounts.

Top-level diagnostic over the per-account routing state
(``teatree.credential_config`` + the ``AnthropicTokenUsage`` health cache): it
enumerates every configured ``pass`` entry (the per-overlay OAuth + API-key lists
plus global) and reports each account's org id, unified 5h / weekly utilization,
extra-usage balance, the 5h next-window reset and weekly reset, and health status,
best account first. Every account is PROBED — an operator asking for token health is
asking what is true now, and a cached row can be stale in every field. ``--cached`` is
the opt-in inverse: render the stored routing verdict and probe nothing. The token that
signs a probe is never rendered.

The rows are routed through the machine-output seam — JSON on stdout under
``--json``, the human table on stderr — and returned as the typed payload.

``--pick`` is the routing verb rather than a report: it resolves the SELECTED OAuth
account through the same selector every dispatch uses (pinning it, so a following
dispatch shares the account and its warm prompt-cache prefix) and prints that account's
``pass`` ENTRY PATH — never a token. It is what lets an interactive session ride the same
per-account routing an agent gets automatically::

    CLAUDE_CODE_OAUTH_TOKEN="$(pass show "$(t3 tokens --pick)")" claude
"""

from typing import IO, Annotated, TypedDict, cast

import typer

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.token_report import TokenAccountPayload


class PickPayload(TypedDict):
    """The selected account's ``pass`` entry path and the scope it was selected for."""

    pass_path: str
    scope: str


_ADHOC_HELP = (
    "Ad-hoc Anthropic token to health-probe as an extra row (repeatable) — for checking a "
    "freshly-minted token before saving it. Warning: a token on the command line is visible "
    "in 'ps' output and your shell history."
)
_CACHED_HELP = (
    "Render the stored routing verdict instead of probing — the view of what the account "
    "selector believes. Values may be stale; the default report always probes live."
)
_PICK_HELP = (
    "Print the SELECTED OAuth account's pass entry path (never a token) and pin it, so an "
    "interactive shell rides the same routing a dispatched agent gets."
)
_SCOPE_HELP = "Routing scope to select for; defaults to the active overlay (T3_OVERLAY_NAME)."


class Command(MachineOutputCommand):
    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human table."),
        ] = False,
        tokens: Annotated[list[str] | None, typer.Option("--token", help=_ADHOC_HELP)] = None,
        cached: Annotated[bool, typer.Option("--cached", help=_CACHED_HELP)] = False,
        pick: Annotated[bool, typer.Option("--pick", help=_PICK_HELP)] = False,
        scope: Annotated[str, typer.Option("--scope", help=_SCOPE_HELP)] = "",
    ) -> list[TokenAccountPayload] | PickPayload:
        """Show per-account Anthropic 5h / weekly token utilization + status."""
        from teatree.token_report import TokenReport, render_table  # noqa: PLC0415 — deferred: lazy command import

        if pick:
            return self._pick(json_output=json_output, scope=scope)

        rows = TokenReport(ad_hoc_tokens=tokens, from_cache=cached).rows()
        payload = [row.as_dict() for row in rows]
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=render_table(rows, from_cache=cached),
        )
        return payload

    def _pick(self, *, json_output: bool, scope: str) -> PickPayload:
        """Resolve, pin and print the selected OAuth account's ``pass`` entry path."""
        # Deferred so the report path pays no import cost for the routing selector.
        from teatree.credential_config import (  # noqa: PLC0415 — deferred: lazy command import
            PassPathSelector,
            TokenKind,
            active_overlay_scope,
        )

        selected_scope = scope or active_overlay_scope()
        chosen = PassPathSelector().select(TokenKind.OAUTH, selected_scope)
        if chosen is None:
            # `typer.Exit` under `call_command` is swallowed and the process exits 0, so a
            # real refusal would report green. The reason is written BEFORE raising, or the
            # operator gets a bare failure with nothing to act on.
            self.stderr.write(
                f"no OAuth account is configured for scope {selected_scope!r} — set anthropic_oauth_pass_paths"
            )
            raise SystemExit(1)

        payload = PickPayload(pass_path=chosen, scope=selected_scope)
        self.print_result = False
        # The human rendering and the machine value are ONE token here, so stdout carries
        # both: nothing is echoed to stderr that a shell substitution would then miss.
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            human=chosen,
            err=cast("IO[str]", self.stdout),
        )
        return payload
