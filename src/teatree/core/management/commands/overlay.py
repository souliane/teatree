"""Overlay inspection: ``t3 <overlay> overlay config [--key KEY]``."""

from pathlib import Path

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay import OverlayConfig
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree_env import _declared_core_keys
from teatree.utils.compose_contract import check_contract


def _config_fields() -> tuple[str, ...]:
    """Derive config fields from OverlayConfig class annotations."""
    return tuple(OverlayConfig.__annotations__)


class Command(TyperCommand):
    @command()
    def config(self, key: str = typer.Option("", help="Show a single config key's value")) -> str:
        """Show overlay configuration."""
        cfg = get_overlay().config
        fields = _config_fields()

        if key:
            if key not in fields:
                return f"Unknown config key: {key}. Available: {', '.join(fields)}"
            return str(getattr(cfg, key))

        return "\n".join(f"{field}: {getattr(cfg, field)}" for field in fields)

    @command()
    def info(self) -> str:
        """Show overlay class path."""
        overlay = get_overlay()
        return f"{type(overlay).__module__}.{type(overlay).__qualname__}"

    @command("contract-check")
    def contract_check(
        self,
        compose: str = typer.Option(
            "",
            "--compose",
            help="Comma-separated paths to docker-compose*.yml files.",
        ),
        allow: str = typer.Option(
            "",
            "--allow",
            help="Comma-separated shell-env keys exempt from the contract.",
        ),
    ) -> str:
        """Fail if compose templates reference keys with no declared producer.

        The producer set is core (``_declared_core_keys``) plus the active
        overlay's ``declared_env_keys()``.  Anything else must either have a
        ``${VAR:-default}`` fallback or appear in ``--allow``.
        """
        paths = [Path(p) for p in (s.strip() for s in compose.split(",")) if p]
        if not paths:
            msg = "--compose is required (comma-separated for multiple files)"
            raise typer.BadParameter(msg)

        missing = [p for p in paths if not p.is_file()]
        if missing:
            missing_str = ", ".join(str(p) for p in missing)
            msg = f"compose file(s) not found: {missing_str}"
            raise typer.BadParameter(msg)

        overlay = get_overlay()
        produced = _declared_core_keys() | overlay.declared_env_keys()
        allowed = {k.strip() for k in allow.split(",") if k.strip()}

        violations = check_contract(paths, produced=produced, allowed=allowed)
        if not violations:
            return f"ok — {len(paths)} compose file(s) check clean against {len(produced)} declared keys"
        for v in violations:
            typer.echo(v.format(), err=True)
        raise typer.Exit(code=1)
