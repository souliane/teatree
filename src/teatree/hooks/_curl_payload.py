"""Curl ``-d``/``--data``/``--json`` and ``-F``/``--form`` payload extraction.

Split out of :mod:`teatree.hooks._command_parser` to keep that module under the
module-health LOC cap. This module owns one concern: pulling the published body
fragments out of a ``curl`` command's data / multipart-form flags, JSON-aware and
fail-closing on the file-reference forms (``@file`` / ``<file``) the gate cannot
read at PreToolUse scan time. :func:`_json_body_fields` is shared with the
``gh``/``glab api`` ``--input`` reader in ``_command_parser``.

Imports DOWN into the dependency-free :mod:`_parser_primitives` leaf
(``FAIL_CLOSED_SENTINEL``, ``attached_value``), so ``_command_parser`` imports
this module without a cycle.
"""

import json
from typing import Final

from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL, attached_value

# Curl long-option data flags — payload is JSON-or-text.
_CURL_DATA_LONG_FLAGS: Final[frozenset[str]] = frozenset(
    {"--data", "--data-raw", "--data-binary", "--data-urlencode", "--json"},
)

# Curl multipart-form flags (``-F 'text=leak'`` / ``--form field=value``). The
# field VALUE is a published body fragment; a ``@file`` / ``<file`` value reads a
# file whose content the gate cannot see at PreToolUse scan time, so it fails
# closed. ``curl -F`` is a distinct surface from ``gh``/``git`` ``-F`` -- this
# walker only runs for a ``curl`` leader (#F7.7).
_CURL_FORM_FLAGS: Final[frozenset[str]] = frozenset({"-F", "--form", "--form-string"})


def _json_body_fields(blob: str) -> list[str]:
    """Return ``text``/``message``/``body`` values from a JSON blob, if any."""
    try:
        decoded = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, dict):
        return []
    return [str(decoded[key]) for key in ("text", "message", "body") if key in decoded]


def _scan_curl_payload(raw: str, payloads: list[str]) -> None:
    """Append ``raw`` plus its JSON ``text``/``message``/``body`` fields.

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


def _record_curl_value(value: str, payloads: list[str]) -> None:
    """Route a single curl data value to the payload list.

    ``@file`` references fail closed (we cannot read arbitrary files);
    everything else gets the standard JSON-aware scan.
    """
    if value.startswith("@"):
        payloads.append(FAIL_CLOSED_SENTINEL)
    else:
        _scan_curl_payload(value, payloads)


def _curl_long_flag_attached(word: str) -> str | None:
    """Return the value of ``--data=VALUE`` / ``--json=VALUE`` if attached."""
    for flag in _CURL_DATA_LONG_FLAGS:
        attached = attached_value(word, flag + "=")
        if attached is not None:
            return attached
    return None


def _curl_short_d_attached(word: str) -> str | None:
    """Return the value of ``-dVALUE`` attached short option, if applicable.

    Excludes the long ``--data*`` / ``--json`` family — those start with
    ``--`` and are handled by :func:`_curl_long_flag_attached`.
    """
    if word.startswith(("--data", "--json")):
        return None
    return attached_value(word, "-d")


def _record_curl_form(field: str, payloads: list[str]) -> None:
    """Route a single curl ``-F``/``--form`` ``name=value`` field to the payload list.

    The published body fragment is the part AFTER the first ``=``. A value that
    reads a FILE (``@path`` uploads a file, ``<path`` reads a field value from a
    file) is unresolvable at PreToolUse scan time, so it fails closed. A field
    with no ``=`` is malformed and contributes nothing (#F7.7).
    """
    _name, sep, value = field.partition("=")
    if not sep:
        return
    if value.startswith(("@", "<")):
        payloads.append(FAIL_CLOSED_SENTINEL)
    else:
        payloads.append(value)


def _curl_form_attached(word: str) -> str | None:
    """Return the attached value of ``-Fname=value`` / ``--form=name=value``, if any."""
    for flag in _CURL_FORM_FLAGS:
        prefix = flag + "=" if flag.startswith("--") else flag
        attached = attached_value(word, prefix)
        if attached is not None:
            return attached
    return None


def _walk_curl_args(words: list[str], payloads: list[str]) -> None:
    """Extract curl ``-d``/``--data*``/``--json`` and ``-F``/``--form`` payloads.

    Supports:
    - ``-d value`` (next token)
    - ``-dvalue`` (attached short option, POSIX)
    - ``-d=value`` (equals form)
    - ``--data value`` / ``--data=value``
    - ``-d@file`` / ``--data @file`` (fail closed — we cannot read the file)
    - ``-F name=value`` / ``--form name=value`` / ``-Fname=value`` multipart form
        fields → the value is a body fragment; ``name=@file`` / ``name=<file``
        fails closed (#F7.7).
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word == "-d" and i + 1 < n:
            _record_curl_value(words[i + 1], payloads)
            i += 2
            continue
        if word in _CURL_DATA_LONG_FLAGS and i + 1 < n:
            _record_curl_value(words[i + 1], payloads)
            i += 2
            continue
        if word in _CURL_FORM_FLAGS and i + 1 < n:
            _record_curl_form(words[i + 1], payloads)
            i += 2
            continue
        attached_short = _curl_short_d_attached(word)
        if attached_short is not None:
            _record_curl_value(attached_short, payloads)
            i += 1
            continue
        attached_long = _curl_long_flag_attached(word)
        if attached_long is not None:
            _record_curl_value(attached_long, payloads)
            i += 1
            continue
        attached_form = _curl_form_attached(word)
        if attached_form is not None:
            _record_curl_form(attached_form, payloads)
        i += 1
