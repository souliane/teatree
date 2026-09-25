"""Private persistence for a managed ChatGPT Codex credential cache."""

import asyncio
import base64
import binascii
import fcntl
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Protocol

from teatree.paths import data_dir_root
from teatree.utils.secrets import SecretNotFoundError, SecretStoreError, read_pass_required, write_pass_with_backup

CODEX_AUTH_PASS_ENTRY = "teatree/codex/auth-json-b64"  # noqa: S105 -- identifier, not a credential
_LOCK_RETRY_SECONDS = 0.05


class GuardedPassWriter(Protocol):
    def __call__(self, key: str, value: str, *, echo: Callable[[str], object]) -> str: ...


class CodexAuthCacheError(RuntimeError):
    @classmethod
    def pass_read_failed(cls, entry: str) -> "CodexAuthCacheError":
        return cls(f"Could not read Codex auth from pass entry {entry!r}.")

    @classmethod
    def invalid_pass_entry(cls, entry: str) -> "CodexAuthCacheError":
        return cls(f"Codex auth requires exactly one base64 line in pass entry {entry!r}.")

    @classmethod
    def pass_persist_failed(cls, entry: str) -> "CodexAuthCacheError":
        return cls(f"Could not persist refreshed Codex auth to pass entry {entry!r}.")

    @classmethod
    def pass_verification_failed(cls, entry: str) -> "CodexAuthCacheError":
        return cls(f"Could not verify Codex auth written to pass entry {entry!r}.")

    @classmethod
    def missing_cache(cls) -> "CodexAuthCacheError":
        return cls("Codex auth cache disappeared before it could be persisted.")

    @classmethod
    def invalid_base64(cls) -> "CodexAuthCacheError":
        return cls("Codex auth cache is not valid base64.")

    @classmethod
    def invalid_json(cls) -> "CodexAuthCacheError":
        return cls("Codex auth cache is not valid JSON.")

    @classmethod
    def invalid_json_object(cls) -> "CodexAuthCacheError":
        return cls("Codex auth cache must be a JSON object.")


def validate_auth_json(raw: bytes) -> None:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexAuthCacheError.invalid_json() from exc
    if not isinstance(decoded, dict):
        raise CodexAuthCacheError.invalid_json_object()


def encode_auth_cache(raw: bytes) -> str:
    validate_auth_json(raw)
    return base64.b64encode(raw).decode("ascii")


def decode_auth_cache(value: str) -> bytes:
    if not value or value.strip() != value or "\n" in value or "\r" in value:
        raise CodexAuthCacheError.invalid_pass_entry(CODEX_AUTH_PASS_ENTRY)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CodexAuthCacheError.invalid_base64() from exc
    validate_auth_json(raw)
    return raw


def _ignore_message(message: str) -> None:
    del message


def resolve_codex_home(code_home: Path | None = None) -> Path:
    if code_home is not None:
        return code_home
    configured = os.environ.get("T3_CODEX_HOME")
    return Path(configured) if configured else data_dir_root() / "codex-home"


def _prepare_private_home(code_home: Path) -> None:
    code_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    code_home.chmod(0o700)


@contextmanager
def _exclusive_lock(code_home: Path) -> Iterator[None]:
    _prepare_private_home(code_home)
    fd = os.open(code_home / ".auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def _acquire_lock(fd: int) -> None:
    acquired = False
    while not acquired:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            await asyncio.sleep(_LOCK_RETRY_SECONDS)
        else:
            acquired = True


def store_auth_cache(
    raw: bytes,
    *,
    code_home: Path | None = None,
    pass_read: Callable[[str], str] = read_pass_required,
    pass_write_with_backup: GuardedPassWriter = write_pass_with_backup,
    echo: Callable[[str], object] = _ignore_message,
) -> None:
    store_auth_cache_from_reader(
        lambda: raw,
        code_home=code_home,
        pass_read=pass_read,
        pass_write_with_backup=pass_write_with_backup,
        echo=echo,
    )


def store_auth_cache_from_reader(
    read: Callable[[], bytes],
    *,
    code_home: Path | None = None,
    pass_read: Callable[[str], str] = read_pass_required,
    pass_write_with_backup: GuardedPassWriter = write_pass_with_backup,
    echo: Callable[[str], object] = _ignore_message,
) -> None:
    """Read and store an auth cache while holding the session writer lock."""
    entry = CODEX_AUTH_PASS_ENTRY
    with _exclusive_lock(resolve_codex_home(code_home)):
        encoded = encode_auth_cache(read())
        try:
            pass_write_with_backup(entry, encoded, echo=echo)
            persisted = pass_read(entry)
        except (SecretNotFoundError, SecretStoreError, OSError) as exc:
            raise CodexAuthCacheError.pass_persist_failed(entry) from exc
        if persisted != encoded:
            raise CodexAuthCacheError.pass_verification_failed(entry)


class CodexAuthCache:
    def __init__(
        self,
        code_home: Path,
        *,
        entry: str = CODEX_AUTH_PASS_ENTRY,
        pass_read: Callable[[str], str] = read_pass_required,
        pass_write_with_backup: GuardedPassWriter = write_pass_with_backup,
    ) -> None:
        self.code_home = code_home
        self.entry = entry
        self.pass_read = pass_read
        self.pass_write_with_backup = pass_write_with_backup
        self._hydrated: bytes | None = None

    @property
    def auth_path(self) -> Path:
        return self.code_home / "auth.json"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Path]:
        await asyncio.to_thread(self._prepare_home)
        fd = os.open(self.code_home / ".auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        try:
            await _acquire_lock(fd)
            await asyncio.to_thread(self.hydrate)
            body_error: BaseException | None = None
            try:
                yield self.auth_path
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — preserve any body failure over cleanup
                body_error = error
            try:
                await self._finish_persist()
            except CodexAuthCacheError as persist_error:
                if body_error is not None:
                    raise body_error from persist_error
                raise
            if body_error is not None:
                raise body_error
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def _finish_persist(self) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.persist))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def hydrate(self) -> Path:
        try:
            value = self.pass_read(self.entry)
        except (SecretNotFoundError, SecretStoreError, OSError) as exc:
            raise CodexAuthCacheError.pass_read_failed(self.entry) from exc
        raw = decode_auth_cache(value)
        self._atomic_write(raw)
        self._hydrated = raw
        return self.auth_path

    def persist(self) -> None:
        try:
            # Codex may replace rather than rewrite its cache while refreshing.
            # Reassert the private mode before reading or retaining that file.
            self.auth_path.chmod(0o600)
            raw = self.auth_path.read_bytes()
        except OSError as exc:
            raise CodexAuthCacheError.missing_cache() from exc
        encoded = encode_auth_cache(raw)
        if raw == self._hydrated:
            return
        try:
            self.pass_write_with_backup(self.entry, encoded, echo=_ignore_message)
        except (SecretStoreError, OSError) as exc:
            raise CodexAuthCacheError.pass_persist_failed(self.entry) from exc
        try:
            persisted = self.pass_read(self.entry)
        except (SecretNotFoundError, SecretStoreError, OSError) as exc:
            raise CodexAuthCacheError.pass_verification_failed(self.entry) from exc
        if persisted != encoded:
            raise CodexAuthCacheError.pass_verification_failed(self.entry)
        self._hydrated = raw

    def _prepare_home(self) -> None:
        _prepare_private_home(self.code_home)

    def _atomic_write(self, raw: bytes) -> None:
        self._prepare_home()
        fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=self.code_home)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.auth_path)
            self.auth_path.chmod(0o600)
            directory_fd = os.open(self.code_home, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
