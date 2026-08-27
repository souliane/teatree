"""What the snapshot puts where a withheld value was, and how a reader spots one.

The withhold DECISION — including the walk into a value's nested keys — is
:func:`~teatree.core.config_interchange.secret_guard.withhold`, shared with the config
export. What differs is the substitution, and the two surfaces need opposite things: an
export DROPS the field, because a stub written back by an import would overwrite the target's
own value; a snapshot SUBSTITUTES a stub, because comparing two boxes is the whole point and
a digest compares without carrying the value.
"""

from collections.abc import Mapping, Sequence
from typing import cast

from teatree.core.config_interchange.secret_guard import Withheld, withhold
from teatree.core.settings_snapshot.serialisation import Json, redaction_stub, serialise


def redact(key: str, value: object, terms: tuple[str, ...]) -> tuple[Json, tuple[Withheld, ...]]:
    """*value* as JSON, with every part the guard withholds replaced by its stub."""
    # The guard walks `object` because its substitution is the caller's; ours takes JSON in and
    # returns a JSON stub, and the walk only rebuilds dicts and lists, so the result is JSON.
    guarded = withhold(key, serialise(value), terms, substitute=redaction_stub)
    return cast("Json", guarded.value), guarded.withheld


def carries_stub(value: object) -> bool:
    """Whether *value* holds a redaction stub anywhere inside it, not just at its top level."""
    if isinstance(value, Mapping):
        return "__redacted__" in value or any(carries_stub(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(carries_stub(item) for item in value)
    return False


__all__ = ["carries_stub", "redact"]
