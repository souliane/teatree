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
already emitted through this seam. Subclass :class:`MachineOutputCommand` so that
pin survives the ``call_command(..., stdout=...)`` path too, and call a child command
through :func:`call_command_streamed` so the wrapper survives from the CALLER's side.
"""

import dataclasses
import datetime
import enum
import json
from collections.abc import Callable
from typing import IO, TextIO, cast

from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import BaseCommand
from django_typer.management import OutputWrapper, TyperCommand


def _install_stream_wrappers(command: BaseCommand, **streams: IO[str] | None) -> None:
    """Put django-typer's disable-capable wrapper on *command*'s named streams.

    Django's own wrapper has no ``disable`` flag and never casts, so it both defeats
    ``print_result = False`` and raises ``AttributeError`` on a non-``str`` return.

    django-stubs narrows the wrapper's ``out`` to ``TextIO``; ``IO[str]`` is what every
    caller here actually holds, and is what Django itself accepts.
    """
    for name, stream in streams.items():
        if stream is None:
            continue
        wrapper = OutputWrapper(cast("TextIO", stream))
        wrapper.style_func = getattr(command, name).style_func
        setattr(command, name, wrapper)


def call_command_streamed(command: str | BaseCommand, *args: str, stream: IO[str]) -> object:
    """``call_command``, both of the child's channels captured to *stream*.

    Passing ``stdout=``/``stderr=`` instead makes ``BaseCommand.execute`` swap in
    Django's wrapper, which then calls ``.endswith`` on the child's typed return —
    so capturing a child's output crashed the caller on any non-``str`` it handed
    back (souliane/teatree#4467). Install the wrapper on the command and withhold the
    options Django would clobber it with. ``command`` takes a registered name or an
    instance, mirroring ``call_command`` itself.
    """
    resolved = command if isinstance(command, BaseCommand) else load_command_class(get_commands()[command], command)
    _install_stream_wrappers(resolved, stdout=stream, stderr=stream)
    return call_command(resolved, *args)


class MachineOutputCommand(TyperCommand):
    """A ``TyperCommand`` whose handlers own stdout through :func:`emit`.

    ``BaseCommand.execute`` replaces ``self.stdout`` with **Django's**
    ``OutputWrapper`` whenever a stream is passed as ``call_command(...,
    stdout=buf)``. That wrapper has no ``disable`` flag, so django-typer's
    ``print_result = False`` becomes a silent no-op and the typed return is
    written to stdout *after* ``emit`` already wrote the JSON there — two
    documents on the channel a front-end parses. Installing django-typer's
    disable-capable wrapper here, and withholding the option Django would
    clobber it with, keeps the pin effective on every call path.

    The pin itself stays per-handler (``self.print_result = False``): a sibling
    verb in the same command group may still return a human ``str`` for
    django-typer to print, and a class-level pin would silence it.
    """

    def execute(self, *args: object, **options: object) -> object:
        _install_stream_wrappers(
            self,
            stdout=cast("IO[str] | None", options.pop("stdout", None)),
            stderr=cast("IO[str] | None", options.pop("stderr", None)),
        )
        return super().execute(*args, **options)


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


__all__ = ["MachineOutputCommand", "call_command_streamed", "emit", "to_jsonable"]
