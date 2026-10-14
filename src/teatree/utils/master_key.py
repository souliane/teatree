"""The one configured source of the database sealing key."""

import base64
import os
import secrets as std_secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from teatree.paths import control_db_dir
from teatree.utils.sealing import MasterKey
from teatree.utils.secrets import SecretStoreError

SOURCE_ENV = "T3_MASTER_KEY_SOURCE"
PRIVATE_FILE_MODE = 0o600


class MasterKeySourceError(SecretStoreError, ValueError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"master key at {source} {reason}")


class MasterKeySource(Protocol):
    @property
    def label(self) -> str: ...

    def load(self) -> MasterKey: ...


class KeyFileSource:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise MasterKeySourceError(SOURCE_ENV, "requires an absolute file path")
        self.path = path

    @property
    def label(self) -> str:
        return f"file:{self.path}"

    def load(self) -> MasterKey:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise MasterKeySourceError(self.label, f"is unreadable: {type(exc).__name__}") from exc
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            mode = stat.S_IMODE(info.st_mode)
            if not stat.S_ISREG(info.st_mode) or mode & 0o077:
                raise MasterKeySourceError(self.label, f"has mode {mode:04o}; use chmod 600")
            raw = stream.read()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise MasterKeySourceError(self.label, "is not ASCII") from exc
        return MasterKey.from_text(text, source=self.label)

    def create(self) -> MasterKey:
        try:
            return self.load()
        except ValueError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
        text = base64.b64encode(os.urandom(32)).decode()
        temporary = self.path.with_name(f".{self.path.name}.{std_secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        except OSError as exc:
            raise MasterKeySourceError(self.label, f"could not be created: {type(exc).__name__}") from exc
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                pass
            else:
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return self.load()


def master_key_source(env: Mapping[str, str] = os.environ) -> KeyFileSource:
    raw = env.get(SOURCE_ENV, "")
    if not raw:
        raw = f"file:{control_db_dir(env) / 'master.key'}"
    if not raw.startswith("file:"):
        raise MasterKeySourceError(SOURCE_ENV, "must be file:<absolute path>")
    path = Path(raw.removeprefix("file:"))
    if not path.is_absolute():
        raise MasterKeySourceError(SOURCE_ENV, "requires an absolute file path")
    return KeyFileSource(path)
