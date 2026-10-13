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
from teatree.hooks._parser_primitives import BODY_FIELD_NAMES, FAIL_CLOSED_SENTINEL, NAMED_FAIL_CLOSED_PREFIX

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
_CURL_TABLE = json.loads(Path(__file__).with_name("curl_options.json").read_text(encoding="utf-8"))["options"]
CURL_LONG_KINDS: Final[dict[str, str]] = {f"--{name}": kind for name, kind, _ in _CURL_TABLE}
CURL_SHORT_KINDS: Final[dict[str, str]] = {f"-{letter}": kind for _, kind, letter in _CURL_TABLE if letter}
_CURL_METHOD_FLAGS: Final[frozenset[str]] = frozenset({"-X", "--request"})
_CURL_IMPLIED_METHODS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("PUT", CURL_UPLOAD_FLAGS),
    ("POST", CURL_FORM_FLAGS),
    ("HEAD", frozenset({"-I", "--head"})),
    ("GET", frozenset({"-G", "--get"})),
    ("POST", CURL_DATA_FLAGS),
)


@dataclass(frozen=True, slots=True)
class CurlOption:
    """One option of a curl argv; ``prefix`` is what precedes an attached value inside ``raw``."""

    name: str
    value: str | None = None
    raw: str = ""
    prefix: str = ""
    expands: bool = False
    unknown: bool = False

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
    expands = spelled.startswith("--expand-")
    negated = spelled.startswith("--no-")
    name = spelled.replace("--expand-", "--", 1).replace("--no-", "--", 1)
    kind = CURL_LONG_KINDS.get(name, "unknown")
    unknown = kind == "unknown" or (expands and kind != "value") or (negated and kind != "boolean")
    if eq:
        return CurlOption(name, attached, raw, f"{spelled}=", expands=expands, unknown=unknown), False
    if kind == "value" and following and not unknown:
        return CurlOption(name, *following, expands=expands), True
    return CurlOption(name, expands=expands, unknown=unknown), False


def _short_cluster(word: str, raw: str, following: tuple[str, str] | None) -> tuple[list[CurlOption], bool]:
    options: list[CurlOption] = []
    for at, letter in enumerate(word[1:], start=1):
        name = f"-{letter}"
        kind = CURL_SHORT_KINDS.get(name, "unknown")
        if kind != "value":
            options.append(CurlOption(name, unknown=kind == "unknown"))
            continue
        if attached := word[at + 1 :]:
            options.append(CurlOption(name, attached.removeprefix("="), raw, word[: at + 1]))
            return options, False
        options.append(CurlOption(name, *following) if following else CurlOption(name))
        return options, following is not None
    return options, False


def curl_argv(words: list[str], raws: list[str] | None = None) -> Iterator[CurlOption | str]:
    spans = raws or words
    i = 0
    options_ended = False
    while i < len(words):
        word, raw = words[i], spans[i]
        following = (words[i + 1], spans[i + 1]) if i + 1 < len(words) else None
        if word == "--" and not options_ended:
            options_ended = True
        elif options_ended:
            yield word
        elif word.startswith("--") and len(word) > len("--"):
            option, consumed = _long_option(word, raw, following)
            yield option
            i += consumed
        elif word.startswith("-") and len(word) > 1:
            options, consumed = _short_cluster(word, raw, following)
            yield from options
            i += consumed
        else:
            yield word
        i += 1


def curl_options(words: list[str], raws: list[str] | None = None) -> Iterator[CurlOption]:
    """Every parsed option of a curl argv, in order."""
    for item in curl_argv(words, raws):
        if isinstance(item, CurlOption):
            yield item


def curl_groups(words: list[str], raws: list[str] | None = None) -> list[tuple[list[CurlOption], list[str]]]:
    groups: list[tuple[list[CurlOption], list[str]]] = [([], [])]
    for item in curl_argv(words, raws):
        if isinstance(item, CurlOption):
            if not item.unknown and item.name in {"--next", "-:"}:
                groups.append(([], []))
            else:
                groups[-1][0].append(item)
                if item.name == "--url" and item.value:
                    groups[-1][1].append(item.value)
        else:
            groups[-1][1].append(item)
    return groups


def curl_method(options: list[CurlOption]) -> str:
    explicit = [option.value for option in options if option.name in _CURL_METHOD_FLAGS and option.value]
    if explicit:
        return explicit[-1].strip("'\"").upper()
    names = {option.name for option in options}
    return next((method for method, implying in _CURL_IMPLIED_METHODS if names & implying), "GET")


def _json_body_fields(blob: str) -> list[str]:
    """Return the body-bearing values a JSON blob carries, if any."""
    try:
        decoded = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, dict):
        return []
    return [str(decoded[key]) for key in _JSON_BODY_KEYS if key in decoded]


def _scan_curl_payload(raw: str, payloads: list[str], *, fail_closed: bool = True) -> None:
    """Append ``raw`` plus every body-bearing JSON field it decodes to.

    A non-JSON-decodable body that LOOKS like JSON (starts with ``{`` or
    ``[``) fails closed because we cannot be sure the gate's pattern
    catalogue covers the obfuscation.
    """
    payloads.append(raw)
    json_fields = _json_body_fields(raw)
    if json_fields:
        payloads.extend(json_fields)
    elif fail_closed and raw.strip().startswith(("{", "[")):
        payloads.append(FAIL_CLOSED_SENTINEL)


def _record_curl_form(
    field: str, raw: str, payloads: list[str], base: Path | None, *, fail_closed: bool = True
) -> None:
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
        if fail_closed:
            payloads.append(FAIL_CLOSED_SENTINEL)
        return
    name_at = raw.find(f"{name}=")
    if name_at < 0:
        payloads.append(resolve_inline_body_value(value, base))
    else:
        payloads.append(resolve_inline_body_value(value, base, raw, name_at + len(name) + 1))


def _walk_curl_args(
    words: list[str],
    payloads: list[str],
    *,
    raws: list[str] | None = None,
    base: Path | None = None,
    group_targets: dict[int, bool] | None = None,
) -> None:
    """Extract every body a curl argv publishes: data values, form fields and uploaded files.

    Each value resolves like an inline body (``$(cat <path>)``, a whole ``$VAR``,
    a live substitution failing closed) against its source span in ``raws``.
    """
    for group_index, (options, _) in enumerate(curl_groups(words, raws)):
        for option in options:
            forge_bound = group_targets is None or group_targets.get(group_index, False)
            publish_bound = group_targets is None or group_index in group_targets
            if option.unknown and forge_bound:
                payloads.append(
                    f"{NAMED_FAIL_CLOSED_PREFIX}curl option {option.name} is outside curl's option table; "
                    "spell every option out in full"
                )
            elif option.name in {"-K", "--config"} and forge_bound:
                payloads.append(
                    f"{NAMED_FAIL_CLOSED_PREFIX}curl reads more options from a config file "
                    f"{option.value or '(missing)'} the gate cannot see"
                )
            if option.value is None or option.name not in _CURL_BODY_FLAGS:
                continue
            if option.name in CURL_UPLOAD_FLAGS or option.reads_a_file():
                if forge_bound if option.name in CURL_UPLOAD_FLAGS else publish_bound:
                    payloads.append(FAIL_CLOSED_SENTINEL)
            elif option.name in CURL_FORM_FLAGS:
                _record_curl_form(option.value, option.raw, payloads, base, fail_closed=publish_bound)
            else:
                _scan_curl_payload(option.resolved(base), payloads, fail_closed=publish_bound)
