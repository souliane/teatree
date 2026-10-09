"""``t3 setup`` step: confirm teatree's own MCP server is wired (#2863)."""

from pathlib import Path

import typer

from teatree.core.mcp_registration import verify_teatree_mcp_registration


class McpServerRegistrar:
    """Report whether the plugin-bundled ``.mcp.json`` declares ``teatree``.

    Read-only and idempotent — ``.mcp.json`` ships committed in the repo, so
    there is nothing for ``t3 setup`` to write; this step only confirms the
    file survived intact and warns loudly if it did not (a hand-edit or a
    merge conflict could otherwise silently regress agents back to shelling
    out to the CLI for structured reads).
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def verify(self) -> bool:
        outcome = verify_teatree_mcp_registration(self.repo)
        prefix = "OK   " if outcome.ok else "WARN "
        typer.echo(f"{prefix} {outcome.message}")
        return outcome.ok
