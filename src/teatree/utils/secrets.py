"""Secret storage via the ``pass`` password store."""

import logging

from teatree.utils.run import CommandFailedError, run_checked

logger = logging.getLogger(__name__)


class SecretNotFoundError(RuntimeError):
    """A required secret is absent, empty, or unreadable in the ``pass`` store.

    Raised by :func:`read_pass_required` so a misconfigured credential fails at
    the point of misconfiguration — naming the key and how to set it — instead
    of surfacing later as an unauthenticated request against a remote service.
    The three constructors distinguish the operator's fix: the store has no
    entry, the entry is empty, or the ``pass`` tool is not installed.
    """

    @classmethod
    def absent(cls, key: str) -> "SecretNotFoundError":
        return cls(f"secret {key!r} has no entry in the `pass` password store — set it with `pass insert {key}`")

    @classmethod
    def empty(cls, key: str) -> "SecretNotFoundError":
        return cls(f"secret {key!r} is empty in the `pass` password store — set it with `pass insert {key}`")

    @classmethod
    def tool_missing(cls, key: str) -> "SecretNotFoundError":
        return cls(
            f"cannot read secret {key!r}: the `pass` password store is not installed "
            f"(install `pass`, then run `pass insert {key}`)"
        )


def read_pass(key: str) -> str:
    """Read a secret from the ``pass`` password store.

    Returns the first line of the stored value, or an empty string
    if the key is not found or ``pass`` is not installed.

    This reader cannot distinguish an absent secret from an empty one — both are
    ``""``. When the caller cannot function without the value, use
    :func:`read_pass_required`; for a genuinely-optional secret with a fallback,
    use :func:`read_pass_or_default`.
    """
    try:
        result = run_checked(["pass", "show", key])
    except (CommandFailedError, FileNotFoundError):
        return ""
    lines = result.stdout.strip().splitlines()
    return lines[0] if lines else ""


def read_pass_required(key: str) -> str:
    """Read a required secret, raising :class:`SecretNotFoundError` when absent.

    The fail-loud variant of :func:`read_pass`. Distinguishes the two operator
    fixes in the raised message: a missing ``pass`` binary
    (``FileNotFoundError`` → install it) from an absent/empty entry
    (``CommandFailedError`` or an empty value → ``pass insert <key>``) — today
    both collapse into the same silent ``""``.
    """
    try:
        result = run_checked(["pass", "show", key])
    except FileNotFoundError as exc:
        raise SecretNotFoundError.tool_missing(key) from exc
    except CommandFailedError as exc:
        raise SecretNotFoundError.absent(key) from exc
    lines = result.stdout.strip().splitlines()
    value = lines[0] if lines else ""
    if not value:
        raise SecretNotFoundError.empty(key)
    return value


def read_pass_or_default(key: str, default: str) -> str:
    """Return the secret at *key*, or *default* with a logged warning when absent.

    For a genuinely-optional secret: the fallback stays available, but taking it
    is a VISIBLE event (a ``WARNING`` naming the key) rather than the invisible
    empty-string fallback :func:`read_pass` gives, so a deliberate default is
    never mistaken for a configured value.
    """
    value = read_pass(key)
    if not value:
        logger.warning("secret %r not found in the `pass` password store — using the provided default", key)
        return default
    return value


def write_pass(key: str, value: str) -> bool:
    """Store *value* under *key* in the ``pass`` password store.

    Uses ``pass insert --multiline --force`` so the secret is read from
    stdin and an existing entry is overwritten silently. Returns ``True``
    on success, ``False`` if ``pass`` is not installed or the call failed.
    """
    try:
        run_checked(["pass", "insert", "--multiline", "--force", key], stdin_text=value)
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def remove_pass(key: str) -> bool:
    """Remove *key* from the ``pass`` password store.

    Uses ``pass rm --force`` so the entry is deleted without prompting.
    Returns ``True`` on success, ``False`` if the entry was absent, ``pass``
    is not installed, or the call failed.
    """
    try:
        run_checked(["pass", "rm", "--force", key])
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def pass_entry_exists(key: str) -> bool:
    """Return ``True`` when *key* resolves to a non-empty entry in ``pass``."""
    return bool(read_pass(key))
