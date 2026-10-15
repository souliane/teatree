import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.db.write_domain import ControlDbWriteDomain

JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
_JOURNAL_COMMAND = re.compile(r"^\s*PRAGMA\s+(?:[A-Za-z_]\w*\.)?journal_mode\s*(?:=|\()", re.IGNORECASE)


class ControlDbJournal:
    def __init__(self, db_path: Path, env: Mapping[str, str] = os.environ) -> None:
        self.db_path = db_path
        self.env = env

    @property
    def mode(self) -> str:
        override = self.env.get("T3_CONTROL_DB_JOURNAL_MODE")
        if override is not None:
            if override not in {"truncate", "wal"}:
                message = "T3_CONTROL_DB_JOURNAL_MODE must be 'truncate' or 'wal'"
                raise ImproperlyConfigured(message)
            return override
        if not self.db_path.is_absolute() or self.db_path.is_symlink() or self.db_path.parent.is_symlink():
            return "truncate"
        return "truncate" if ControlDbWriteDomain(self.db_path, env=self.env).on_host_filesystem else "wal"

    @property
    def reason(self) -> str:
        if "T3_CONTROL_DB_JOURNAL_MODE" in self.env:
            return "operator override"
        if self.mode == "wal":
            return "control-DB volume"
        return "host-reachable filesystem"

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
            commands.extend(["PRAGMA synchronous=NORMAL", f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}"])
        return commands
