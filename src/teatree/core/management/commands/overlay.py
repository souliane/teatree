"""Overlay inspection: ``t3 <overlay> overlay config [--key KEY]``."""

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay_loader import get_overlay

# Fields exposed by ``overlay config``.
_CONFIG_FIELDS = (
    "gitlab_url",
    "github_owner",
    "github_project_number",
    "require_ticket",
    "mr_close_ticket",
    "mr_auto_labels",
    "known_variants",
    "frontend_repos",
    "workspace_repos",
    "protected_branches",
    "dev_env_url",
    "dashboard_logo",
)


class Command(TyperCommand):
    @command()
    def config(self, key: str = typer.Option("", help="Show a single config key's value")) -> str:
        """Show overlay configuration."""
        cfg = get_overlay().config

        if key:
            if key not in _CONFIG_FIELDS:
                return f"Unknown config key: {key}. Available: {', '.join(_CONFIG_FIELDS)}"
            return str(getattr(cfg, key))

        return "\n".join(f"{field}: {getattr(cfg, field)}" for field in _CONFIG_FIELDS)

    @command()
    def info(self) -> str:
        """Show overlay class path."""
        overlay = get_overlay()
        return f"{type(overlay).__module__}.{type(overlay).__qualname__}"
