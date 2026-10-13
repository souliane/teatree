"""The curl argv grammar, and the body fragments a curl write publishes.

Split out of :mod:`teatree.hooks._command_parser` to keep that module under the
module-health LOC cap. :func:`curl_options` reads an argv the way curl does, one
option per item however it is spelled (``-sd x``, ``-sdx``, ``--data=x``,
``--expand-data x``), so the method detector in :mod:`_forge_write_detection` and the
payload walker here read the same options. A body curl reads from a file or expands at
run time cannot be seen at PreToolUse scan time, so it fails closed.
:func:`_json_body_fields` is shared with the ``gh``/``glab api`` ``--input`` reader in
``_command_parser``.

Imports DOWN into the dependency-free :mod:`_parser_primitives` leaf, so
``_command_parser`` imports this module without a cycle.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks._inline_body_resolution import resolve_attached_value, resolve_inline_body_value
from teatree.hooks._parser_primitives import BODY_FIELD_NAMES, FAIL_CLOSED_SENTINEL

# The argv walkers' catalogue plus Slack's ``text``, which has no CLI-flag form.
_JSON_BODY_KEYS: Final[tuple[str, ...]] = (*sorted(BODY_FIELD_NAMES), "text")

# Curl data flags — payload is JSON-or-text.
CURL_DATA_FLAGS: Final[frozenset[str]] = frozenset(
    {"-d", "--data", "--data-ascii", "--data-raw", "--data-binary", "--data-urlencode", "--json"},
)

# Curl multipart-form flags (``-F 'text=leak'`` / ``--form field=value``). The
# field VALUE is a published body fragment; a ``@file`` / ``<file`` value reads a
# file whose content the gate cannot see at PreToolUse scan time, so it fails
# closed. ``curl -F`` is a distinct surface from ``gh``/``git`` ``-F`` -- this
# walker only runs for a ``curl`` leader (#F7.7).
CURL_FORM_FLAGS: Final[frozenset[str]] = frozenset({"-F", "--form", "--form-string"})

CURL_UPLOAD_FLAGS: Final[frozenset[str]] = frozenset({"-T", "--upload-file"})

_CURL_BODY_FLAGS: Final[frozenset[str]] = CURL_DATA_FLAGS | CURL_FORM_FLAGS | CURL_UPLOAD_FLAGS
_CURL_LONG_VALUE_FLAGS: Final[frozenset[str]] = _CURL_BODY_FLAGS | {"--request"}

# curl's short options that take an argument; any other letter of a ``-sSfL`` cluster is a switch.
_CURL_SHORT_VALUE_LETTERS: Final[frozenset[str]] = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")


@dataclass(frozen=True, slots=True)
class CurlOption:
    """One option of a curl argv; ``prefix`` is what precedes an attached value inside ``raw``."""

    name: str
    value: str | None = None
    raw: str = ""
    prefix: str = ""
    expands: bool = False

    def resolved(self, base: Path | None) -> str:
        if self.prefix:
            return resolve_attached_value(self.value or "", base, self.raw, self.prefix)
        return resolve_inline_body_value(self.value or "", base, self.raw)

    def reads_a_file(self) -> bool:
        value = self.value or ""
        if self.expands and "{{" in value:
            return True
        if self.name == "--data-urlencode":
            return "@" in value.partition("=")[0]
        return self.name in CURL_DATA_FLAGS and value.startswith("@")


def _long_option(word: str, raw: str, following: tuple[str, str] | None) -> tuple[CurlOption, bool]:
    spelled, eq, attached = word.partition("=")
    name = spelled.replace("--expand-", "--", 1)
    expands = name != spelled
    if eq:
        return CurlOption(name, attached, raw, f"{spelled}=", expands=expands), False
    if name in _CURL_LONG_VALUE_FLAGS and following:
        return CurlOption(name, *following, expands=expands), True
    return CurlOption(name, expands=expands), False


def _short_cluster(word: str, raw: str, following: tuple[str, str] | None) -> tuple[list[CurlOption], bool]:
    options: list[CurlOption] = []
    for at, letter in enumerate(word[1:], start=1):
        if letter not in _CURL_SHORT_VALUE_LETTERS:
            options.append(CurlOption(f"-{letter}"))
            continue
        if attached := word[at + 1 :]:
            options.append(CurlOption(f"-{letter}", attached.removeprefix("="), raw, word[: at + 1]))
            return options, False
        options.append(CurlOption(f"-{letter}", *following) if following else CurlOption(f"-{letter}"))
        return options, following is not None
    return options, False


def curl_options(words: list[str], raws: list[str] | None = None) -> Iterator[CurlOption]:
    """Every option of a curl argv, in order; positional words (the URLs) are skipped."""
    spans = raws or words
    i = 0
    while i < len(words):
        word, raw = words[i], spans[i]
        following = (words[i + 1], spans[i + 1]) if i + 1 < len(words) else None
        if word.startswith("--") and len(word) > len("--"):
            option, consumed = _long_option(word, raw, following)
            yield option
            i += consumed
        elif word.startswith("-") and len(word) > 1:
            options, consumed = _short_cluster(word, raw, following)
            yield from options
            i += consumed
        i += 1


def _json_body_fields(blob: str) -> list[str]:
    """Return the body-bearing values a JSON blob carries, if any."""
    try:
        decoded = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, dict):
        return []
    return [str(decoded[key]) for key in _JSON_BODY_KEYS if key in decoded]


def _scan_curl_payload(raw: str, payloads: list[str]) -> None:
    """Append ``raw`` plus every body-bearing JSON field it decodes to.

    A non-JSON-decodable body that LOOKS like JSON (starts with ``{`` or
    ``[``) fails closed because we cannot be sure the gate's pattern
    catalogue covers the obfuscation.
    """
    payloads.append(raw)
    json_fields = _json_body_fields(raw)
    if json_fields:
        payloads.extend(json_fields)
    elif raw.strip().startswith(("{", "[")):
        payloads.append(FAIL_CLOSED_SENTINEL)


def _record_curl_form(field: str, raw: str, payloads: list[str], base: Path | None) -> None:
    """Route a single curl ``-F``/``--form`` ``name=value`` field to the payload list.

    The published body fragment is the part AFTER the first ``=``. A value that
    reads a FILE (``@path`` uploads a file, ``<path`` reads a field value from a
    file) is unresolvable at PreToolUse scan time, so it fails closed. A field
    with no ``=`` is malformed and contributes nothing (#F7.7).
    """
    name, sep, value = field.partition("=")
    if not sep:
        return
    if value.startswith(("@", "<")):
        payloads.append(FAIL_CLOSED_SENTINEL)
        return
    name_at = raw.find(f"{name}=")
    if name_at < 0:
        payloads.append(resolve_inline_body_value(value, base))
    else:
        payloads.append(resolve_inline_body_value(value, base, raw, name_at + len(name) + 1))


def _walk_curl_args(
    words: list[str], payloads: list[str], *, raws: list[str] | None = None, base: Path | None = None
) -> None:
    """Extract every body a curl argv publishes: data values, form fields and uploaded files.

    Each value resolves like an inline body (``$(cat <path>)``, a whole ``$VAR``,
    a live substitution failing closed) against its source span in ``raws``.
    """
    for option in curl_options(words, raws):
        if option.value is None or option.name not in _CURL_BODY_FLAGS:
            continue
        if option.name in CURL_UPLOAD_FLAGS or option.reads_a_file():
            payloads.append(FAIL_CLOSED_SENTINEL)
        elif option.name in CURL_FORM_FLAGS:
            _record_curl_form(option.value, option.raw, payloads, base)
        else:
            _scan_curl_payload(option.resolved(base), payloads)
