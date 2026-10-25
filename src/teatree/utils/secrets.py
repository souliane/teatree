"""The secret facade: one reader/writer API over the installed store (the ``pass`` password store by default)."""

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, override

from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_bounded_group
from teatree.utils.secret_backups import backup_name

# ``pass show`` shells out to gpg, which talks to gpg-agent over a unix socket. A
# wedged agent — a GNUPGHOME on a mount that cannot host the sockets, a dead
# keyboxd/scdaemon — leaves that read blocked with no deadline of its own, and
# every caller inherits the hang: on the headless box a stuck agent made every
# ``t3`` invocation take minutes for a full day. A deadline turns the wedge into
# a loud, attributable failure instead of an unbounded wait.
KEYRING_READ_TIMEOUT_SECONDS = 20.0
KEYRING_READ_TIMEOUT_ENV_VAR = "T3_KEYRING_READ_TIMEOUT_SECONDS"

# ``pass show`` exits 1 for "is not in the password store" and reserves every
# other non-zero code for a read that FAILED (gpg exits 2 on "No Keybox daemon
# running"). Those are opposite answers: 1 is a genuine empty, anything else is a
# read that never happened and must never be laundered into one.
PASS_ABSENT_RETURNCODE = 1

SELECTOR_ENV = "T3_SECRET_STORE"


def keyring_read_timeout_seconds() -> float:
    """The per-``pass``-invocation deadline, widenable for a slow smartcard/agent."""
    raw = os.environ.get(KEYRING_READ_TIMEOUT_ENV_VAR, "")
    try:
        override = float(raw)
    except ValueError:
        return KEYRING_READ_TIMEOUT_SECONDS
    return override if override > 0 else KEYRING_READ_TIMEOUT_SECONDS


class SecretStoreError(RuntimeError):
    """A ``pass`` invocation FAILED — the value is unknown, not absent.

    The distinction is the whole point: an absent entry is an answer a caller may
    act on, while a timed-out or undecryptable read is a failure that must reach
    the caller instead of degrading to ``""`` and being consumed as "no secret
    configured". See ``skills/rules`` § "External Read Failure Must Fail Loud".
    """

    @classmethod
    def timed_out(cls, key: str, seconds: float) -> "SecretStoreError":
        return cls(
            f"reading secret {key!r} from the `pass` password store timed out after {seconds:g}s — "
            f"the gpg agent is wedged or unreachable (check $GNUPGHOME, then `gpgconf --kill all`); "
            f"raise {KEYRING_READ_TIMEOUT_ENV_VAR} only when a slow smartcard genuinely needs longer"
        )

    @classmethod
    def refused_empty(cls, key: str, store: "SecretStore | None" = None) -> "SecretStoreError":
        return cls(f"refusing to write an empty value to {key!r} in {(store or PassStore()).label}")

    @classmethod
    def not_backed_up(cls, key: str, backup_key: str) -> "SecretStoreError":
        return cls(
            f"could not back up the existing value of {key!r} to {backup_key!r} — refusing to overwrite "
            f"an entry on a store that is not version-controlled and whose prior value would be unrecoverable"
        )

    @classmethod
    def not_written(cls, key: str) -> "SecretStoreError":
        return cls(f"`pass insert {key}` failed — nothing was stored")

    @classmethod
    def unlisted(cls, prefix: str, error: OSError) -> "SecretStoreError":
        return cls(f"listing {prefix!r} in the `pass` password store failed: {type(error).__name__}")

    @classmethod
    def unreadable(cls, key: str, returncode: int, detail: str) -> "SecretStoreError":
        """The read never completed, so nothing is known about the entry — say only that.

        Claiming "the entry exists but could not be decrypted" asserts a lookup that did
        not happen: gpg exits 2 when its keybox daemon will not start, before any entry is
        reached, and that sends the reader to check the entry rather than the keyring.
        ``$GNUPGHOME`` is quoted because it is the datum that separates a working host
        from a container carrying a different one.
        """
        tail = detail.strip().splitlines()
        hint = f": {tail[-1]}" if tail else ""
        home = os.environ.get("GNUPGHOME") or "<unset>"
        return cls(
            f"reading secret {key!r} from the `pass` password store failed (rc={returncode}){hint} — "
            f"the KEYRING did not answer, so whether the entry exists is unknown. "
            f"GNUPGHOME={home}; try `gpgconf --kill all`, and note a daemon under a "
            f"DIFFERENT GNUPGHOME survives that (`pgrep -al keyboxd gpg-agent`)"
        )


class SecretNotFoundError(RuntimeError):
    """A required secret is absent, empty, or unreadable in the ``pass`` store.

    Raised by :func:`read_pass_required` so a misconfigured credential fails at
    the point of misconfiguration — naming the key and how to set it — instead
    of surfacing later as an unauthenticated request against a remote service.
    The three constructors distinguish the operator's fix: the store has no
    entry, the entry is empty, or the ``pass`` tool is not installed.
    """

    @classmethod
    def absent(cls, key: str, store: "SecretStore | None" = None) -> "SecretNotFoundError":
        active = store or PassStore()
        return cls(f"secret {key!r} has no entry in {active.label} — set it with {active.remedy(key)}")

    @classmethod
    def empty(cls, key: str, store: "SecretStore | None" = None) -> "SecretNotFoundError":
        active = store or PassStore()
        return cls(f"secret {key!r} is empty in {active.label} — set it with {active.remedy(key)}")

    @classmethod
    def tool_missing(cls, key: str) -> "SecretNotFoundError":
        return cls(
            f"cannot read secret {key!r}: the `pass` password store is not installed "
            f"(install `pass`, then run `pass insert {key}`)"
        )


def _pass_show(key: str) -> str | None:
    """The raw value stored at *key*, or ``None`` when the store holds no such entry.

    The one seam every read goes through, so the deadline and the
    absent-versus-unreadable verdict are decided in exactly one place. Raises
    :class:`SecretStoreError` when the read timed out or the entry could not be
    decrypted, and ``FileNotFoundError`` when ``pass`` itself is absent — both
    failures the callers below translate rather than swallow.
    """
    deadline = keyring_read_timeout_seconds()
    try:
        result = run_bounded_group(["pass", "show", key], timeout=deadline)
    except TimeoutExpired as exc:
        raise SecretStoreError.timed_out(key, deadline) from exc
    except CommandFailedError as exc:
        if exc.returncode == PASS_ABSENT_RETURNCODE:
            return None
        raise SecretStoreError.unreadable(key, exc.returncode, exc.stderr) from exc
    return result.stdout


#: The injected store-read seam: given a ``pass`` entry, return its secret (``""`` when
#: absent). :func:`read_pass` satisfies it; a test maps entries to canned values.
type SecretReader = Callable[[str], str]


def _first_line(value: str | None) -> str:
    lines = value.strip().splitlines() if value is not None else []
    return lines[0] if lines else ""


def read_pass(key: str) -> str:
    """Read a secret from the ``pass`` password store.

    Returns the first line of the stored value, or an empty string when the key
    is not in the store or ``pass`` is not installed. A read that FAILED — a
    wedged gpg agent, an undecryptable entry — raises :class:`SecretStoreError`
    instead: an unknown value is not an absent one, and a caller that cannot tell
    them apart posts with no token and reports success.

    This reader cannot distinguish an absent secret from an empty one — both are
    ``""``. When the caller cannot function without the value, use
    :func:`read_pass_required`.
    """
    try:
        return _first_line(_store().read(key))
    except FileNotFoundError:
        return ""


def read_pass_required(key: str) -> str:
    """Read a required secret, raising :class:`SecretNotFoundError` when absent.

    The fail-loud variant of :func:`read_pass`. Distinguishes the operator's fix
    in the raised message: a missing ``pass`` binary (``FileNotFoundError`` →
    install it) from an absent or empty entry (→ ``pass insert <key>``). A read
    that FAILED propagates as :class:`SecretStoreError` — naming a wedged keyring
    as such rather than as a missing entry.
    """
    store = _store()
    try:
        raw = store.read(key)
    except FileNotFoundError as exc:
        raise SecretNotFoundError.tool_missing(key) from exc
    if raw is None:
        raise SecretNotFoundError.absent(key, store)
    value = _first_line(raw)
    if not value:
        raise SecretNotFoundError.empty(key, store)
    return value


def write_pass(key: str, value: str) -> bool:
    return _store().write(key, value)


def _pass_write(key: str, value: str) -> bool:
    """Store *value* under *key* in the ``pass`` password store.

    Uses ``pass insert --multiline --force`` so the secret is read from
    stdin and an existing entry is overwritten silently. Returns ``True``
    on success, ``False`` if ``pass`` is not installed or the call failed.
    A write that HUNG raises :class:`SecretStoreError` rather than reporting the
    same ``False`` a refused write reports.
    """
    deadline = keyring_read_timeout_seconds()
    try:
        run_bounded_group(["pass", "insert", "--multiline", "--force", key], stdin_text=value, timeout=deadline)
    except TimeoutExpired as exc:
        raise SecretStoreError.timed_out(key, deadline) from exc
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def write_pass_with_backup(key: str, value: str, *, echo: Callable[[str], object]) -> str:
    """Copy any existing value to ``<key>.bak-<UTC stamp>``, then write; return the backup key or ``""``.

    The store is not version-controlled, so an overwrite is irreversible — a
    backup that could not be written aborts the write rather than destroying a
    value nobody can get back.
    """
    if not value.strip():
        raise SecretStoreError.refused_empty(key, _store())
    backup_key = _back_up_existing(key, echo=echo)
    if not write_pass(key, value):
        raise SecretStoreError.not_written(key)
    return backup_key


def _back_up_existing(key: str, *, echo: Callable[[str], object]) -> str:
    existing = read_pass(key)
    if not existing:
        return ""
    backup_key = backup_name(key, datetime.now(UTC))
    try:
        backed_up = write_pass(backup_key, existing)
    except SecretStoreError as exc:
        raise SecretStoreError.not_backed_up(key, backup_key) from exc
    if not backed_up:
        raise SecretStoreError.not_backed_up(key, backup_key)
    echo(f"OK    Backed up the previous value to `pass {backup_key}` before overwriting.")
    return backup_key


def remove_pass(key: str) -> bool:
    return _store().remove(key)


def _pass_remove(key: str) -> bool:
    """Remove *key* from the ``pass`` password store.

    Uses ``pass rm --force`` so the entry is deleted without prompting.
    Returns ``True`` on success, ``False`` if the entry was absent, ``pass``
    is not installed, or the call failed. A removal that HUNG raises
    :class:`SecretStoreError`, on the same reasoning as :func:`write_pass`.
    """
    deadline = keyring_read_timeout_seconds()
    try:
        run_bounded_group(["pass", "rm", "--force", key], timeout=deadline)
    except TimeoutExpired as exc:
        raise SecretStoreError.timed_out(key, deadline) from exc
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def pass_entry_names(prefix: str) -> frozenset[str]:
    return _store().names(prefix)


def _pass_names(prefix: str) -> frozenset[str]:
    """The entry names directly under *prefix*, read from the store directory without decrypting anything."""
    root = Path(os.environ.get("PASSWORD_STORE_DIR") or Path.home() / ".password-store") / prefix
    try:
        with os.scandir(root) as entries:
            return frozenset(
                entry.name.removesuffix(".gpg") for entry in entries if entry.name.endswith(".gpg") and entry.is_file()
            )
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise SecretStoreError.unlisted(prefix, exc) from exc


def pass_entry_exists(key: str) -> bool:
    """Return ``True`` when *key* resolves to a non-empty entry in ``pass``."""
    return bool(read_pass(key))


class SecretStore(Protocol):
    label: str

    def read(self, key: str) -> str | None: ...

    def write(self, key: str, value: str) -> bool: ...

    def remove(self, key: str) -> bool: ...

    def names(self, prefix: str) -> frozenset[str]: ...

    def remedy(self, key: str) -> str: ...


class PassStore(SecretStore):
    label = "the `pass` password store"

    @override
    def read(self, key: str) -> str | None:
        return _pass_show(key)

    @override
    def write(self, key: str, value: str) -> bool:
        return _pass_write(key, value)

    @override
    def remove(self, key: str) -> bool:
        return _pass_remove(key)

    @override
    def names(self, prefix: str) -> frozenset[str]:
        return _pass_names(prefix)

    @override
    def remedy(self, key: str) -> str:
        return f"`pass insert {key}`"


@dataclass(slots=True)
class _StoreSlot:
    active: SecretStore | None = None
    sealed_factory: Callable[[], SecretStore] | None = None

    def install(self, store: SecretStore | None) -> None:
        self.active = store

    def register_sealed(self, factory: Callable[[], SecretStore]) -> None:
        self.sealed_factory = factory

    def resolve(self) -> SecretStore:
        if self.active is None:
            self.active = self._from_selector()
        return self.active

    def _from_selector(self) -> SecretStore:
        choice = os.environ.get(SELECTOR_ENV, "") or "pass"
        if choice == "pass":
            return PassStore()
        if choice != "sealed":
            message = f"{SELECTOR_ENV} must be 'pass' or 'sealed', not {choice!r}"
            raise SecretStoreError(message)
        ensure_django()
        if self.sealed_factory is None:
            message = f"{SELECTOR_ENV}=sealed but the sealed store is not registered (Django apps not ready)"
            raise SecretStoreError(message)
        return self.sealed_factory()


_store_slot = _StoreSlot()


def _store() -> SecretStore:
    return _store_slot.resolve()


install_store = _store_slot.install
register_sealed_store = _store_slot.register_sealed


@contextmanager
def use_store(store: SecretStore | None) -> Generator[SecretStore | None]:
    previous = _store_slot.active
    install_store(store)
    try:
        yield store
    finally:
        install_store(previous)
