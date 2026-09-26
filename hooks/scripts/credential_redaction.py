"""Replace every credential in agent-facing text with ``<redacted>``.

Three nested partitions — runs, a URL's userinfo, then segments — computed left
to right BEFORE and independently of any classification. A segment carrying
``=`` prints up to its first one and redacts the rest, so nothing here decides
where a value ends: a value's extent is a partition index, never a match.

STATED LIMITATION: the truncation marker is not authenticated, so text git echoed
back verbatim can contain one. It is a spoofing surface and no credential passes
through it; making it unforgeable needs a nonce this hook has nowhere to keep.
"""

import string
from typing import Final

REDACTED: Final[str] = "<redacted>"
# ADDING a member is the dangerous direction — it SPLITS a pair or an authority and the
# tail past the split loses its taint; measured, adding `:` leaks on 1134 of 3402 pair
# cells and 140 of 140 userinfo ones. Dropping one only over-redacts, which no cell sees.
_RUN_BOUNDARY: Final[frozenset[str]] = frozenset(" \t\n\r\v\f\x00\xa0'\"`")
# `:` is absent on purpose: it plausibly appears INSIDE a credential, so `?ref=main:tok=X` redacts whole.
_SEGMENT_SEPARATOR: Final[frozenset[str]] = frozenset("&,;|?#")
_USERINFO_STOP: Final[frozenset[str]] = frozenset("/?#@")
_SCHEME_CHARS: Final[frozenset[str]] = frozenset(f"{string.ascii_letters}{string.digits}+.-")
_HTTP_SCHEME_SUFFIXES: Final[tuple[str, str]] = ("http", "https")
_AUTHORITY: Final[str] = "://"
_MAX_SCANNED_CHARS: Final[int] = 65536
# Carries no `=`, no `://` and no segment separator, so scrubbing it is a no-op and
# the refusal's second pass over the assembled text leaves it alone.
_TRUNCATED: Final[str] = f"<truncated-at-{_MAX_SCANNED_CHARS}-chars>"


def redact_credentials(text: str) -> str:
    scanned = _truncated_at_a_run_boundary(text)
    kept: list[str] = []
    start = 0
    for index, char in enumerate(scanned):
        if char in _RUN_BOUNDARY:
            kept.extend((_redact_run(scanned[start:index]), char))
            start = index + 1
    kept.append(_redact_run(scanned[start:]))
    return "".join(kept)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _redact_run(run: str) -> str:
    scanned = _redact_userinfo(run)
    boundaries = [index for index, char in enumerate(scanned) if char in _SEGMENT_SEPARATOR]
    kept: list[str] = []
    pending = ""
    tainted = False
    start = 0
    for index in [*boundaries, len(scanned)]:
        segment = scanned[start:index]
        name, equals, _ = segment.partition("=")
        if equals:
            kept.append(f"{pending}{name}={REDACTED}")
            tainted = True
        else:
            # Past a separator INSIDE a credential there is no name to print, only the
            # value's tail — and a tail dropped in silence reads as text never written.
            kept.append(f"{pending}{REDACTED if tainted else segment}")
        pending = scanned[index : index + 1]
        start = index + 1
    return "".join(kept)


def _redact_userinfo(run: str) -> str:
    kept: list[str] = []
    cursor = 0
    mark = run.find(_AUTHORITY)
    while mark != -1:
        start = mark + len(_AUTHORITY)
        stop = start
        while stop < len(run) and run[stop] not in _USERINFO_STOP:
            stop += 1
        if stop == len(run):
            # A run boundary cut the authority short, so whether what follows `://` is a
            # userinfo is unresolvable — and an unresolvable authority is not published.
            return f"{''.join(kept)}{run[cursor:start]}{REDACTED if stop > start else ''}"
        if run[stop] == "@":
            scheme = mark
            while scheme > 0 and run[scheme - 1] in _SCHEME_CHARS:
                scheme -= 1
            userinfo = run[start:stop]
            login, colon, _ = userinfo.partition(":")
            if run[scheme:mark].lower().endswith(_HTTP_SCHEME_SUFFIXES):
                userinfo = REDACTED
            elif colon:
                userinfo = f"{login}:{REDACTED}"
            kept.append(f"{run[cursor:start]}{userinfo}")
            cursor = stop
        mark = run.find(_AUTHORITY, start)
    kept.append(run[cursor:])
    return "".join(kept)


def _truncated_at_a_run_boundary(text: str) -> str:
    """*text* cut back to a run boundary, with what was dropped SAID rather than erased.

    A cut inside a run could sever a pair's `=`, so it lands on a boundary and whole
    runs go. Unmarked, a 70 KB answer with no boundary in it scrubbed to `""` and the
    refusal quoting it read `exit 128 with no stderr` — a probe that said nothing.
    """
    if len(text) <= _MAX_SCANNED_CHARS:
        return text
    head = text[:_MAX_SCANNED_CHARS]
    # `kept` is empty or ends AT a boundary, so the marker is already its own run.
    kept = head[: max(head.rfind(char) for char in _RUN_BOUNDARY) + 1]
    return f"{kept}{_TRUNCATED}"
