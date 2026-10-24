"""Which agent tools this host can actually run, so strict setup waits only on their plugins."""

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath

import typer
from django.db import DatabaseError

from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY, resolve_codex_home
from teatree.cli.setup.codex_plugin_staging import codex_home
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential
from teatree.utils.secrets import SecretStoreError, pass_entry_names

_ANTHROPIC_SPECS = (AnthropicApiKeyCredential.spec, AnthropicSubscriptionCredential.spec)
_CLAUDE_LOGIN_FILE = (".claude", ".credentials.json")
_CODEX_AUTH_PASS = PurePosixPath(CODEX_AUTH_PASS_ENTRY)


class AgentTool(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class StrictPluginGate:
    """A tool without credentials never runs, so its failed plugin must not fail strict setup.

    No tool credentialed gates on every tool: the host is unconfigured, not credential-free by design.
    A source that cannot be read counts as held, so an uncertain probe tightens the gate.
    """

    def passes(self, registered: Mapping[AgentTool, bool]) -> bool:
        probes = {AgentTool.CLAUDE: self._claude_held, AgentTool.CODEX: self._codex_held}
        gating = {tool for tool, held in probes.items() if held()} or set(AgentTool)
        blocking: list[AgentTool] = []
        for tool in AgentTool:
            if registered[tool]:
                continue
            if tool in gating:
                blocking.append(tool)
            else:
                typer.echo(
                    f"WARN  {tool} plugin registration failed, but no {tool} credentials were detected; "
                    "strict setup does not wait on it."
                )
        if blocking:
            typer.echo(
                f"ERROR Strict setup is not ready: plugin registration failed for {', '.join(blocking)}.", err=True
            )
        return not blocking

    @classmethod
    def _claude_held(cls) -> bool:
        return (
            any(os.environ.get(spec.env_var) for spec in _ANTHROPIC_SPECS)
            or cls._present(Path.home().joinpath(*_CLAUDE_LOGIN_FILE))
            or cls._routing_configured()
        )

    @classmethod
    def _codex_held(cls) -> bool:
        if any(cls._present(home / "auth.json") for home in (codex_home(), resolve_codex_home())):
            return True
        try:
            return _CODEX_AUTH_PASS.name in pass_entry_names(_CODEX_AUTH_PASS.parent.as_posix())
        except SecretStoreError:
            return True

    @staticmethod
    def _routing_configured() -> bool:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred: ORM needs the app registry

        try:
            routing = ConfigSetting.objects.filter(key__in=[spec.routing_setting for spec in _ANTHROPIC_SPECS])
            return any(row.value for row in routing)
        except DatabaseError:
            return True

    @staticmethod
    def _present(path: Path) -> bool:
        try:
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True
        return True
