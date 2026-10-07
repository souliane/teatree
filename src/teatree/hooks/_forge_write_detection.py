"""A forge write that carries free text is a publish the pre-publish gates scan.

One flag table drives both halves: :func:`segment_is_forge_cli_write` decides that a
``gh``/``glab`` segment publishes, and :func:`walk_forge_free_text` extracts what it
publishes, so detection and extraction cannot drift apart. The polarity is fail
closed: an unknown verb is a write, and is a publish as soon as it carries free text.
Reads stay unscanned: a read verb, a non-publishing command group, a GET/HEAD
``curl`` and a graphql query document.
"""

import re
from dataclasses import dataclass
from itertools import starmap
from typing import Final

from teatree.hooks._body_file_resolution import BODY_FILE_FLAG_NAMES, STDIN_DASH, BodyFileContext, append_file_payload
from teatree.hooks._curl_payload import _CURL_DATA_LONG_FLAGS, _CURL_FORM_FLAGS
from teatree.hooks._inline_body_resolution import resolve_attached_value, resolve_inline_body_value
from teatree.hooks._parser_primitives import (
    BODY_LONG_OPTION_FIELDS,
    BODY_SHORT_FLAGS,
    GLAB_BODY_SHORT_FLAGS,
    attached_api_field,
    canonical_leader,
    read_file_arg,
    strip_wrapper_prefix,
)
from teatree.hooks._publish_detection import segment_word_lists
from teatree.hooks._python_rest_detection import find_python_forge_rest_urls

_FORGE_CLI_LEADERS: Final[frozenset[str]] = frozenset({"gh", "glab"})
_COMMAND_PATH_DEPTH: Final[int] = 2
_SHORT_FLAG_LEN: Final[int] = len("-x")

# Spellings from ``gh``/``glab <verb> --help``. ``-d`` is boolean on some verbs, which is
# why the walker advances one token at a time and never skips a value.
_FREE_TEXT_LONG: Final[frozenset[str]] = frozenset(
    {"--body", "--title", "--description", "--message", "--notes", "--comment", "--desc", "--subject", "--name"}
)
_FREE_TEXT_SHORT: Final[frozenset[str]] = frozenset({"-b", "-m", "-t", "-n", "-N", "-c", "-d"})
_DETECTED_LONG: Final[frozenset[str]] = _FREE_TEXT_LONG | BODY_FILE_FLAG_NAMES
_FREE_TEXT_FLAGS: Final[frozenset[str]] = _DETECTED_LONG | _FREE_TEXT_SHORT
_WALKED_LONG: Final[frozenset[str]] = _FREE_TEXT_LONG - {f"--{name}" for name in BODY_LONG_OPTION_FIELDS}
_FIELD_FLAGS: Final[frozenset[str]] = frozenset({"-f", "-F", "--field", "--raw-field"})

_READ_VERBS: Final[frozenset[str]] = frozenset(
    {"list", "ls", "view", "diff", "checks", "status", "get", "export", "download", "watch", "trace"}
    | {"checkout", "clone"}
)
# ``secret`` values are stored encrypted and never rendered back to a reader.
_NON_PUBLISHING_GROUPS: Final[frozenset[str]] = frozenset(
    {"auth", "config", "alias", "completion", "help", "version", "browse", "search", "status", "extension", "secret"}
)
_GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"-R", "--repo", "--hostname"})

_CONTENT_FILE_VERBS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {("gh", "gist", "create"), ("glab", "snippet", "create")}
)
_CONTENT_ADD_VERBS: Final[frozenset[tuple[str, str, str]]] = frozenset({("gh", "gist", "edit")})
_CONTENT_ADD_FLAGS: Final[frozenset[str]] = frozenset({"-a", "--add"})
_CONTENT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-d", "--desc", "--description", "-t", "--title", "-f", "--filename", "-v", "--visibility", "-R", "--repo"}
)

_GRAPHQL_MUTATION_RE: Final[re.Pattern[str]] = re.compile(r"\bmutation\b")

_CURL_METHOD_FLAGS: Final[frozenset[str]] = frozenset({"-X", "--request"})
_CURL_IMPLIED_METHODS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("HEAD", frozenset({"-I", "--head"})),
    ("GET", frozenset({"-G", "--get"})),
    ("PUT", frozenset({"-T", "--upload-file"})),
    ("POST", frozenset({"-d", *_CURL_DATA_LONG_FLAGS, *_CURL_FORM_FLAGS})),
)
_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
_FORGE_HOSTS: Final[tuple[str, ...]] = ("github.com", "gitlab.com")
_URL_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://)?(?:[^/@\s]+@)?(?P<host>[A-Za-z0-9.-]+)(?::\d+)?(?:[/?#]|$)", re.IGNORECASE
)


@dataclass(frozen=True)
class _ForgeCall:
    leader: str
    group: str
    verb: str
    tokens: tuple[str, ...]
    args: tuple[str, ...]


def _forge_call(words: list[str]) -> _ForgeCall | None:
    rest = strip_wrapper_prefix(words)
    if not rest or canonical_leader(rest[0]) not in _FORGE_CLI_LEADERS:
        return None
    found: list[int] = []
    i = 1
    while i < len(rest) and len(found) < _COMMAND_PATH_DEPTH:
        if rest[i] in _GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if not rest[i].startswith("-"):
            found.append(i)
        i += 1
    group, verb = ([rest[at] for at in found] + ["", ""])[:2]
    args = rest[found[-1] + 1 :] if found else rest[1:]
    return _ForgeCall(canonical_leader(rest[0]), group, verb, tuple(rest), tuple(args))


def _attached_free_text(word: str, long_flags: frozenset[str]) -> tuple[str, str] | None:
    """Return ``(prefix, value)`` of an attached ``--long=value`` / ``-sVALUE`` free-text flag."""
    if word.startswith("--"):
        flag, sep, value = word.partition("=")
        return (f"{flag}=", value) if sep and flag in long_flags else None
    prefix = word[:_SHORT_FLAG_LEN]
    if len(word) > _SHORT_FLAG_LEN and prefix in _FREE_TEXT_SHORT:
        return prefix, word[_SHORT_FLAG_LEN:].removeprefix("=")
    return None


def _token_carries_free_text(word: str, nxt: str) -> bool:
    if word in _FREE_TEXT_FLAGS or _attached_free_text(word, _DETECTED_LONG) is not None:
        return True
    if word in _FIELD_FLAGS:
        return word == "-F" or "=" in nxt
    return attached_api_field(word) is not None or word.startswith("-F")


def segment_is_forge_cli_write(words: list[str]) -> bool:
    """Return True iff a ``gh``/``glab`` segment is a non-read verb that publishes free text or file content."""
    call = _forge_call(words)
    if call is None or call.group == "api" or call.group in _NON_PUBLISHING_GROUPS or call.verb in _READ_VERBS:
        return False
    if (call.leader, call.group, call.verb) in _CONTENT_FILE_VERBS | _CONTENT_ADD_VERBS:
        return True
    tokens = call.tokens
    return any(starmap(_token_carries_free_text, zip(tokens, [*tokens[1:], ""], strict=True)))


def _content_operands(words: list[str]) -> list[str]:
    """The files a content verb publishes: its positionals (stdin when none), or ``gist edit --add`` values."""
    call = _forge_call(words)
    if call is None:
        return []
    key = (call.leader, call.group, call.verb)
    if key in _CONTENT_ADD_VERBS:
        return _added_files(call.args)
    if key not in _CONTENT_FILE_VERBS:
        return []
    operands: list[str] = []
    takes_value = False
    for word in call.args:
        if not takes_value and (word == STDIN_DASH or not word.startswith("-")):
            operands.append(word)
        takes_value = not takes_value and word in _CONTENT_VALUE_FLAGS
    return operands or [STDIN_DASH]


def _added_files(args: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for word, nxt in zip(args, [*args[1:], ""], strict=True):
        if word in _CONTENT_ADD_FLAGS and nxt:
            files.append(nxt)
        elif word.startswith("--add="):
            files.append(word.removeprefix("--add="))
    return files


def segment_reads_stdin_content(words: list[str]) -> bool:
    return STDIN_DASH in _content_operands(words)


def walk_forge_free_text(
    words: list[str], raws: list[str], payloads: list[str], ctx: BodyFileContext, leader: str
) -> None:
    """Append the free text and file content a ``gh``/``glab`` write publishes.

    Only the flags the every-segment body walker does not already read are taken
    here, so nothing is extracted twice.
    """
    generic_short = BODY_SHORT_FLAGS | (GLAB_BODY_SHORT_FLAGS if leader == "glab" else frozenset())
    spaced = _WALKED_LONG | (_FREE_TEXT_SHORT - generic_short)
    for i, word in enumerate(words):
        if word in spaced and i + 1 < len(words):
            payloads.append(resolve_inline_body_value(words[i + 1], ctx.base, raws[i + 1]))
        elif (attached := _attached_free_text(word, _WALKED_LONG)) is not None:
            prefix, value = attached
            payloads.append(resolve_attached_value(value, ctx.base, raws[i], prefix))
    for operand in _content_operands(words):
        append_file_payload(operand, payloads, ctx, fail_closed=ctx.fail_closed_body_file, leader=leader)


def graphql_document_is_read(words: list[str]) -> bool:
    """Return True iff a ``graphql`` call's query document is readable and carries no ``mutation``."""
    if "graphql" not in words:
        return False
    for word, nxt in zip(words, [*words[1:], ""], strict=True):
        assignment = nxt if word in _FIELD_FLAGS else attached_api_field(word)
        if assignment is None or not assignment.startswith("query="):
            continue
        document: str | None = assignment.removeprefix("query=")
        if document.startswith("@") and word.startswith(("-F", "--field")):
            document = read_file_arg(document[1:])
        return document is not None and _GRAPHQL_MUTATION_RE.search(document) is None
    return False


def _curl_flag(word: str) -> tuple[str, str | None]:
    if word.startswith("--"):
        flag, sep, value = word.partition("=")
        return flag, value if sep else None
    if word.startswith("-") and len(word) > _SHORT_FLAG_LEN:
        return word[:_SHORT_FLAG_LEN], word[_SHORT_FLAG_LEN:]
    return word, None


def _curl_method(args: list[str]) -> str:
    explicit: str | None = None
    flags: set[str] = set()
    for word, nxt in zip(args, [*args[1:], ""], strict=True):
        flag, attached = _curl_flag(word)
        if flag in _CURL_METHOD_FLAGS:
            explicit = attached if attached is not None else nxt
        flags.add(flag)
    if explicit:
        return explicit.strip("'\"").upper()
    return next((method for method, implying in _CURL_IMPLIED_METHODS if flags & implying), "GET")


def _url_host(url: str) -> str:
    match = _URL_HOST_RE.match(url)
    return match["host"].lower() if match else ""


def _is_forge_host(host: str) -> bool:
    return any(host == forge or host.endswith(f".{forge}") for forge in _FORGE_HOSTS)


def _curl_urls(args: list[str]) -> list[str]:
    """Every URL a ``curl`` names; a scheme-less one only when its host is a forge."""
    urls: list[str] = []
    for word in args:
        candidate = word.removeprefix("--url=")
        if candidate.lower().startswith(("http://", "https://")):
            urls.append(candidate)
        elif _is_forge_host(_url_host(candidate)):
            urls.append(f"https://{candidate}")
    return urls


def curl_forge_targets(words: list[str]) -> list[tuple[str, str] | None]:
    """One ``(forge, slug)`` per URL a ``curl`` segment names, ``None`` where a URL is no forge project."""
    rest = strip_wrapper_prefix(words)
    if not rest or canonical_leader(rest[0]) != "curl":
        return []
    return [next(find_python_forge_rest_urls(url), None) for url in _curl_urls(rest[1:])]


def _segment_is_curl_forge_write(words: list[str]) -> bool:
    rest = strip_wrapper_prefix(words)
    if not rest or canonical_leader(rest[0]) != "curl" or _curl_method(rest[1:]) in _READ_METHODS:
        return False
    return any(
        _is_forge_host(_url_host(url)) or next(find_python_forge_rest_urls(url), None) is not None
        for url in _curl_urls(rest[1:])
    )


def command_has_forge_free_text_publish(command: str) -> bool:
    return any(
        segment_is_forge_cli_write(words) or _segment_is_curl_forge_write(words)
        for words in segment_word_lists(command)
    )
