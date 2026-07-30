"""Which compose services pin the agent output root, and which inherit a fallback.

Agent transcripts and scratch output land under ``$TMPDIR``. The entrypoint
exports it (``deploy/entrypoint.sh``), which covers a service's MAIN process —
but ``docker exec`` into a running service does NOT run the entrypoint, so an
exec'd process falls back to the default temp root and its output lands under a
second, ephemeral root. Consumers that scan transcripts (dream/consolidation,
retro tooling, forensics) then have to scan BOTH roots or silently miss half the
data (souliane/teatree#3641).

Pinning ``TMPDIR`` in each service's compose ``environment`` fixes it at the
source every process in the container inherits, exec'd or not. This module is
the pure predicate; :mod:`teatree.cli.doctor.checks_loop` reports it.
"""

from pathlib import Path
from typing import Any

import yaml

OUTPUT_ROOT_ENV = "TMPDIR"


def _declared_env_keys(service: dict[str, Any]) -> set[str]:
    """The env var names a compose service declares inline (mapping OR list form)."""
    environment = service.get("environment")
    if isinstance(environment, dict):
        return set(environment)
    if isinstance(environment, list):
        return {str(entry).split("=", 1)[0] for entry in environment}
    return set()


def services_missing_output_root(compose_file: Path) -> list[str]:
    """Service names in *compose_file* that do not declare ``TMPDIR`` inline.

    An unreadable or malformed compose file yields ``[]`` — the caller is an
    advisory doctor check, and a parse failure is not evidence of a missing pin.
    """
    try:
        parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(parsed, dict):
        return []
    services = parsed.get("services")
    if not isinstance(services, dict):
        return []
    return [
        name
        for name, service in services.items()
        if isinstance(service, dict) and OUTPUT_ROOT_ENV not in _declared_env_keys(service)
    ]
