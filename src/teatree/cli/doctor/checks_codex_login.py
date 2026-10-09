"""The doctor's one advisory Codex line: login present or missing, its age, the last long hold, a stale route.

Stat and database reads only: ``auth.json`` is never opened, so no credential can reach the output.
"""

from datetime import UTC, datetime
from pathlib import Path

import typer

_CODEX_HARNESS = "codex_app_server"
_REASON_CHARS = 90


def _codex_route_configured() -> bool:
    from teatree.config.agent_spawn import resolve_agent_config  # noqa: PLC0415 — deferred: keeps CLI startup light

    return any(
        isinstance(policy, tuple) and any(candidate.harness == _CODEX_HARNESS for candidate in policy)
        for policy in resolve_agent_config().skill_models.values()
    )


def _last_long_hold() -> str:
    from teatree.agents.skill_routing import QUOTA_AUTH_HOLD  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.models import AgentRouteAvailability  # noqa: PLC0415 — ORM import needs the app registry

    held = AgentRouteAvailability.objects.filter(harness=_CODEX_HARNESS).exclude(unavailable_reason="")
    for row in held.order_by("-observed_at"):
        if row.retry_at - row.observed_at >= QUOTA_AUTH_HOLD:
            return f"; last hold: {row.unavailable_reason[:_REASON_CHARS]} (until {row.retry_at:%Y-%m-%d %H:%MZ})"
    return ""


def _report(auth_json: Path) -> None:
    from teatree.core.models import UsageWindowState  # noqa: PLC0415 — ORM import needs the app registry

    route = _codex_route_configured()
    hold = _last_long_hold()
    if not (route or hold or auth_json.is_file()):
        return
    if auth_json.is_file():
        modified = datetime.fromtimestamp(auth_json.stat().st_mtime, tz=UTC)
        typer.echo(f"OK    Codex login present (auth.json modified {modified:%Y-%m-%d %H:%MZ}){hold}")
    else:
        typer.echo(f"WARN  Codex login missing: run `t3 codex auth import`, then `t3 codex auth check`{hold}")
    if route and not UsageWindowState.objects.active().exists():
        typer.echo(
            "WARN  A Codex route is configured while no usage window is active: the route row outlived the usage "
            "window. Clear it with `t3 <overlay> config_setting clear agent_skill_models` unless Codex is "
            "meant to stay a standing candidate."
        )


def check_codex_login() -> bool:
    from teatree.agents.codex_auth_cache import resolve_codex_home  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        _report(resolve_codex_home() / "auth.json")
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Codex login check crashed: {exc.__class__.__name__}: {exc}")
    return True
