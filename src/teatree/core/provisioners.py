"""Generic provisioning utilities extracted from overlay implementations.

Overlays provide configuration via TypedDict specs (SymlinkSpec, ServiceSpec,
DbImportStrategy). This module provides reusable engines that operate on
those specs, so overlays don't need to re-implement symlink application,
Docker Compose orchestration, or settings injection.
"""

import logging
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path

from teatree.core.overlay import ServiceSpec, SymlinkSpec

logger = logging.getLogger(__name__)


# ── Symlink Engine ──────────────────────────────────────────────────


def apply_symlinks(specs: list[SymlinkSpec], base_dir: str | Path) -> list[str]:
    """Apply symlink/copy specs relative to *base_dir*.

    Returns a list of paths that were created or updated.
    """
    base = Path(base_dir)
    created: list[str] = []

    for spec in specs:
        path_str = spec.get("path", "")
        source_str = spec.get("source", "")
        mode = spec.get("mode", "symlink")

        if not path_str or not source_str:
            continue

        target = base / path_str
        source = Path(source_str)

        target.parent.mkdir(parents=True, exist_ok=True)

        if mode == "symlink":
            _apply_symlink(target, source)
        elif mode in {"copy", "copy-and-patch"}:
            _apply_copy(target, source)
        else:
            logger.warning("Unknown symlink mode %r for %s", mode, target)
            continue

        created.append(str(target))

    return created


def _apply_symlink(target: Path, source: Path) -> None:
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


def _apply_copy(target: Path, source: Path) -> None:
    if source.is_file():
        shutil.copy2(source, target)
    elif source.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


# ── Service Orchestrator ────────────────────────────────────────────


def start_services(
    specs: dict[str, ServiceSpec],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Start services defined by ServiceSpec dicts.

    Returns a mapping of service name to success status.
    """
    run_env = {**os.environ, **(env or {})}
    results: dict[str, bool] = {}

    for name, spec in specs.items():
        command = spec.get("start_command", [])
        if not command:
            compose_file = spec.get("compose_file", "")
            service = spec.get("service", name)
            if compose_file:
                command = ["docker", "compose", "-f", compose_file, "up", "-d", service]
            else:
                logger.warning("No start_command or compose_file for service %s", name)
                results[name] = False
                continue

        try:
            proc = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                check=False,
                env=run_env,
            )
            if proc.returncode != 0:
                logger.warning("Service %s failed to start: %s", name, proc.stderr[:500])
                results[name] = False
            else:
                results[name] = True
        except FileNotFoundError:
            logger.warning("Command not found for service %s: %s", name, command[0])
            results[name] = False

    return results


# ── Settings Injector ───────────────────────────────────────────────


def inject_settings(target_file: Path, settings: dict[str, str], *, header: str = "") -> None:
    """Append or update key=value pairs in a settings file.

    If *header* is provided, settings are written under that header comment.
    Existing keys are updated in place; new keys are appended.
    """
    existing_lines: list[str] = []
    if target_file.is_file():
        existing_lines = target_file.read_text(encoding="utf-8").splitlines()

    existing_keys = {}
    for i, line in enumerate(existing_lines):
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            existing_keys[key] = i

    for key, value in settings.items():
        new_line = f"{key}={value}"
        if key in existing_keys:
            existing_lines[existing_keys[key]] = new_line
        else:
            header_line = f"# {header}"
            if header and not any(line.strip() == header_line for line in existing_lines):
                existing_lines.append(f"\n# {header}")
            existing_lines.append(new_line)

    target_file.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
