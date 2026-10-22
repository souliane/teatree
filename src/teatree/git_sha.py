"""Validate full Git commit identifiers."""

import re

_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")


def is_full_git_sha(value: str) -> bool:
    return _FULL_GIT_SHA.fullmatch(value) is not None
