import os
import re
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.db.write_domain import ControlDbWriteDomain

JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
_SQLITE_NAME = r'(?:[A-Za-z_]\w*|"[^"]+"|\'[^\']+\'|`[^`]+`|\[[^]]+\])'


def _pragma_command(name: str) -> re.Pattern[str]:
    identifier = rf'(?:{name}|"{name}"|\'{name}\'|`{name}`|\[{name}\])'
    return re.compile(rf"^\s*PRAGMA\s+(?:{_SQLITE_NAME}\s*\.\s*)?{identifier}\s*(?:=|\()", re.IGNORECASE)


_JOURNAL_COMMAND = _pragma_command("journal_mode")
_SYNCHRONOUS_COMMAND = _pragma_command("synchronous")


class ControlDbJournal:
    def __init__(
        self, db_path: Path, env: Mapping[str, str] = os.environ, *, containerized: bool | None = None
    ) -> None:
        self.db_path = db_path
        self.env = env
        self.domain = ControlDbWriteDomain(db_path, env=env, containerized=containerized)

    @cached_property
    def selection(self) -> tuple[str, str]:
        override = self.env.get("T3_CONTROL_DB_JOURNAL_MODE", "").strip().lower()
        if override not in {"", "truncate", "wal"}:
            message = "T3_CONTROL_DB_JOURNAL_MODE must be 'truncate' or 'wal'"
            raise ImproperlyConfigured(message)
        if not self.domain.in_control_dir:
            return "truncate", "outside the control-DB directory"
        if override == "truncate":
            return "truncate", "operator override"
        eligible, reason = self.domain.wal_eligible
        if override == "wal" and not eligible:
            message = f"T3_CONTROL_DB_JOURNAL_MODE=wal refused: {reason}"
            raise ImproperlyConfigured(message)
        return ("wal" if eligible else "truncate"), ("operator override" if override else reason)

    @property
    def mode(self) -> str:
        return self.selection[0]

    @property
    def reason(self) -> str:
        return self.selection[1]

    def init_commands(self, existing: Sequence[str]) -> list[str]:
        mode = self.mode
        selected = f"PRAGMA journal_mode={mode.upper()}"
        commands: list[str] = []
        for command in existing:
            if not command.strip():
                continue
            if _JOURNAL_COMMAND.match(command):
                if selected not in commands:
                    commands.append(selected)
            else:
                commands.append(command)
        if selected not in commands:
            commands.insert(0, selected)
        if mode == "wal":
            for command in ("PRAGMA synchronous=NORMAL", f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}"):
                if command.startswith("PRAGMA synchronous") and any(
                    _SYNCHRONOUS_COMMAND.match(item) for item in commands
                ):
                    continue
                if command not in commands:
                    commands.append(command)
        return commands
