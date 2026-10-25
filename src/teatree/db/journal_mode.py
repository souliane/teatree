import os
import re
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from teatree.db.write_domain import ControlDbWriteDomain
from teatree.paths import DEFAULT_CONTROL_DB_DIR

JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
_JOURNAL_HEADER_OFFSET = 18
_WAL_HEADER_VALUE = 2
_SQLITE_NAME = r'(?:[A-Za-z_]\w*|"[^"]+"|\'[^\']+\'|`[^`]+`|\[[^]]+\])'


def _pragma_command(name: str) -> re.Pattern[str]:
    identifier = rf'(?:{name}|"{name}"|\'{name}\'|`{name}`|\[{name}\])'
    return re.compile(rf"^\s*PRAGMA\s+(?:{_SQLITE_NAME}\s*\.\s*)?{identifier}\s*(?:=|\()", re.IGNORECASE)


_JOURNAL_COMMAND = _pragma_command("journal_mode")
_SYNCHRONOUS_COMMAND = _pragma_command("synchronous")


def sqlite_file_journal_mode(path: Path) -> str | None:
    with path.open("rb") as source:
        header = source.read(_JOURNAL_HEADER_OFFSET + 1)
    if (
        not header.startswith(b"SQLite format 3\x00")
        or len(header) <= _JOURNAL_HEADER_OFFSET
        or header[_JOURNAL_HEADER_OFFSET] not in {1, _WAL_HEADER_VALUE}
    ):
        return None
    return "wal" if header[_JOURNAL_HEADER_OFFSET] == _WAL_HEADER_VALUE else "truncate"


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
        checkout_selection = self._unshipped_checkout_selection(override)
        if checkout_selection is not None:
            return checkout_selection
        if override == "truncate":
            return "truncate", "operator override"
        eligible, reason = self.domain.wal_eligible
        if override == "wal" and not eligible:
            message = f"T3_CONTROL_DB_JOURNAL_MODE=wal refused: {reason}"
            raise ImproperlyConfigured(message)
        return ("wal" if eligible else "truncate"), ("operator override" if override else reason)

    def _unshipped_checkout_selection(self, override: str) -> tuple[str, str] | None:
        source_root = Path(__file__).resolve().parents[3]
        deployed_root = self.env.get("TEATREE_CLONE_DIR")
        unshipped_checkout = (source_root / ".git").exists() and (
            not deployed_root or source_root != Path(deployed_root).resolve()
        )
        if not unshipped_checkout or not self.db_path.is_file():
            return None
        if override and self.db_path.parent != DEFAULT_CONTROL_DB_DIR:
            return None
        try:
            existing_mode = sqlite_file_journal_mode(self.db_path)
        except OSError as error:
            message = "cannot read the existing control DB journal header"
            raise ImproperlyConfigured(message) from error
        if existing_mode is None:
            message = "invalid existing control DB journal header"
            raise ImproperlyConfigured(message)
        if override:
            if override != existing_mode:
                message = "worktree cannot change the installed control DB journal mode"
                raise ImproperlyConfigured(message)
            return None
        if existing_mode == "truncate":
            return "truncate", "checkout preserves existing rollback journal"
        eligible, reason = self.domain.wal_eligible
        if not eligible:
            message = f"checkout cannot safely open existing WAL database: {reason}"
            raise ImproperlyConfigured(message)
        return "wal", "checkout preserves existing WAL journal"

    @property
    def mode(self) -> str:
        return self.selection[0]

    @property
    def reason(self) -> str:
        return self.selection[1]

    def _existing_file_mode(self) -> str | None:
        try:
            return sqlite_file_journal_mode(self.db_path)
        except FileNotFoundError:
            return None

    def init_commands(self, existing: Sequence[str]) -> list[str]:
        mode = self.mode
        selected = f"PRAGMA journal_mode={mode.upper()}"
        pending_setter = self._existing_file_mode() != mode
        commands: list[str] = []
        for command in existing:
            if not command.strip():
                continue
            if _JOURNAL_COMMAND.match(command):
                if pending_setter:
                    commands.append(selected)
                    pending_setter = False
            else:
                commands.append(command)
        if pending_setter:
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
