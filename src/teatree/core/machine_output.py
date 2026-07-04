"""Machine-output seam for management commands (PR-30, front-end-seam keystone).

`t3` is a machine interface: a front-end (Pi, a CI runner, another agent) drives
teatree by shelling to ``t3 ... --json`` and parsing stdout. That only works if
stdout is a PURE data channel — valid JSON under ``--json``, zero human bytes.
django-typer's default reprs a command's typed return onto stdout (single
quotes, ``True``/``False``/``None`` — not JSON), and a command that ALSO writes a
human line or banner to stdout interleaves the two; both defeat ``json.loads``.

``emit`` is the one seam every converted command routes output through:

- under ``--json``: ``json.dumps(payload)`` to stdout, human diagnostics to stderr.
- otherwise: the human view to stderr, so stdout stays a clean JSON channel.

The command ALSO returns ``payload`` unchanged so ``call_command`` consumers keep
getting the typed object; set ``print_result = False`` on the ``TyperCommand`` so
django-typer does not additionally repr the return onto stdout after the handler
already emitted through this seam.
"""

import dataclasses
import datetime
import enum
import json
from collections.abc import Callable
from typing import IO


def _json_default(obj: object) -> object:
    """``json.dumps`` fallback for the non-native types command returns carry.

    ``json.dumps`` handles dict/list/tuple/str/int/float/bool/None natively and
    recurses through them, calling this only for a leaf it cannot serialize —
    enums, datetimes, dataclasses, sets, ``Path``. An unrecognised leaf degrades
    to ``str(obj)`` so serialization is total and never raises mid-command.
    """
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    return str(obj)


def to_jsonable(obj: object) -> object:
    """Return a JSON-serializable structure for a typed command return.

    The structural (non-string) form of what ``emit`` writes — for a consumer
    (a test, PR-18's table renderer) that wants the data, not the JSON text.
    """
    return json.loads(json.dumps(obj, default=_json_default))


def emit(
    payload: object,
    *,
    json_output: bool,
    out: IO[str],
    err: IO[str],
    human: Callable[[IO[str]], None] | str | None = None,
) -> None:
    """Route a command's output: machine JSON to stdout, human view to stderr.

    ``out``/``err`` are the command's ``self.stdout``/``self.stderr`` wrappers (any
    ``.write``-able stream in tests). ``human`` is either a pre-rendered string or
    a renderer callable given the stderr stream (for a rich table that cannot be a
    plain string); ``None`` emits no human view.
    """
    if json_output:
        out.write(json.dumps(payload, default=_json_default))
        return
    if human is None:
        return
    if isinstance(human, str):
        if human:
            err.write(human)
        return
    human(err)


__all__ = ["emit", "to_jsonable"]
