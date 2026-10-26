"""The one configured source of the database sealing key."""

import base64
import errno
import logging
import os
import secrets as std_secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from teatree.paths import control_db_dir
from teatree.utils.sealing import MASTER_KEY_BYTES, MasterKey
from teatree.utils.secrets import SecretStoreError

logger = logging.getLogger(__name__)
_WARNED_IN_VOLUME: set[str] = set()


def reset_master_key_volume_warnings() -> None:
    _WARNED_IN_VOLUME.clear()


SOURCE_ENV = "T3_MASTER_KEY_SOURCE"
ROLE_ENV = "TEATREE_ROLE"
PRIVATE_FILE_MODE = 0o600


class MasterKeySourceError(SecretStoreError, ValueError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"master key at {source} {reason}")


class MasterKeySource(Protocol):
    @property
    def label(self) -> str: ...

    def load(self) -> MasterKey: ...


class KeyFileSource:
    def __init__(self, path: Path, *, volume: Path | None = None) -> None:
        if not path.is_absolute():
            raise MasterKeySourceError(SOURCE_ENV, "requires an absolute file path")
        self.path = path
        self.volume = volume

    @property
    def label(self) -> str:
        return f"file:{self.path}"

    @property
    def in_volume(self) -> bool:
        return self.volume is not None and self.path.resolve().is_relative_to(self.volume.resolve())

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
        self._warn_if_in_volume()
        return MasterKey.from_text(text, source=self.label)

    def _warn_if_in_volume(self) -> None:
        if self.in_volume and self.label not in _WARNED_IN_VOLUME:
            _WARNED_IN_VOLUME.add(self.label)
            logger.warning(
                "master key %s sits inside the control-database volume; a copy of the volume carries the key "
                "with the rows it seals (deploy/README.md)",
                self.label,
            )

    def create(self) -> MasterKey:
        try:
            return self.load()
        except MasterKeySourceError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
        if self.in_volume:
            reason = "would be created inside the control-database volume; point T3_MASTER_KEY_SOURCE outside it"
            raise MasterKeySourceError(self.label, f"{reason} (deploy/README.md)")
        if not self._link_new_key():
            return self.load()
        created = self.load()
        logger.warning("created master key %s with key_id %s; keep a copy of this key", self.label, created.key_id)
        return created

    def _link_new_key(self) -> bool:
        temporary = self.path.with_name(f".{self.path.name}.{std_secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        except OSError as exc:
            hint = " (the key directory is read-only: provision the key on the host, deploy/README.md)"
            raise MasterKeySourceError(
                self.label, f"could not be created: {type(exc).__name__}{hint if exc.errno == errno.EROFS else ''}"
            ) from exc
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                return False
            except OSError as exc:
                raise MasterKeySourceError(self.label, f"could not be created: {type(exc).__name__}") from exc
            self._sync_directory()
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_directory(self) -> None:
        try:
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            reason = f"was created but its directory could not be synced: {type(exc).__name__}"
            raise MasterKeySourceError(self.label, reason) from exc


def master_key_source(env: Mapping[str, str] = os.environ) -> KeyFileSource:
    raw = env.get(SOURCE_ENV, "")
    if not raw:
        raw = f"file:{control_db_dir(env) / 'master.key'}"
    if not raw.startswith("file:"):
        raise MasterKeySourceError(SOURCE_ENV, "must be file:<absolute path>")
    path = Path(raw.removeprefix("file:"))
    if not path.is_absolute():
        raise MasterKeySourceError(SOURCE_ENV, "requires an absolute file path")
    return KeyFileSource(path, volume=control_db_dir(env) if env.get(ROLE_ENV) else None)
