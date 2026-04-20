"""Overlay inspection: ``t3 <overlay> overlay config [--key KEY]``."""

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay import OverlayConfig
from teatree.core.overlay_loader import get_overlay


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
