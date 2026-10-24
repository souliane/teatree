"""Recognize foreground shell waits likely to outlive a prompt-cache lifetime."""

import re

from teatree.hooks._shell_lexer import is_command_separator, tokenize

CACHE_WAIT_LIMIT_SECONDS = 240
_DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_MULTIPLIER = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_PREFIXES = frozenset({"command", "exec", "time", "nohup"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_MAX_SHELL_DEPTH = 2


def _duration_seconds(word: str) -> float | None:
    match = _DURATION.fullmatch(word)
    if match is None:
        return None
    return float(match[1]) * _MULTIPLIER[match[2]]


def _unwrapped(words: list[str]) -> list[str]:
    while words and words[0] in _PREFIXES:
        words = words[1:]
    match words:
        case ["timeout" | "gtimeout", duration, *rest] if (limit := _duration_seconds(duration)) is not None:
            return rest if limit >= CACHE_WAIT_LIMIT_SECONDS else []
    return words


def _sleep_is_long(words: list[str]) -> bool:
    if words[:1] != ["sleep"]:
        return False
    durations = [_duration_seconds(word) for word in words[1:]]
    return (
        bool(durations)
        and None not in durations
        and sum(seconds or 0 for seconds in durations) >= CACHE_WAIT_LIMIT_SECONDS
    )


def _wait_in_words(words: list[str], depth: int) -> str | None:
    words = _unwrapped(words)
    if not words:
        return None
    head = words[0]
    if _sleep_is_long(words):
        return "sleep"
    if head == "gh" and words[1:3] == ["run", "watch"] and not {"--help", "-h"}.intersection(words[3:]):
        return "gh run watch"
    if head == "glab" and words[1:3] == ["ci", "status"] and "--watch" in words[3:] and "--help" not in words[3:]:
        return "glab ci status --watch"
    if head in _SHELLS and depth < _MAX_SHELL_DEPTH:
        for index, word in enumerate(words[1:], start=1):
            if word.startswith("-") and "c" in word[1:] and index + 1 < len(words):
                return _wait_in_command(words[index + 1], depth + 1)
    return None


def _wait_in_command(command: str, depth: int) -> str | None:
    segment: list[str] = []
    for token in tokenize(command):
        if is_command_separator(token):
            if token.value != "&":
                found = _wait_in_words(segment, depth)
                if found is not None:
                    return found
            segment = []
        else:
            segment.append(token.value)
    return _wait_in_words(segment, depth)


def declared_cache_expiring_wait(command: str) -> str | None:
    return _wait_in_command(command, 0)
