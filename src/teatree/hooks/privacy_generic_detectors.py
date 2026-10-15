"""Generic privacy detectors shared by the push scanner and forge write gates."""

import re
from typing import NamedTuple

from teatree.utils.exact_carve_out import mask_exempt


class GenericFinding(NamedTuple):
    category: str
    match: str
    position: int


GENERIC_CATEGORIES = frozenset({"email", "home_path", "private_ip", "api_key", "internal_hostname"})


# Require a plausible local part: it may contain ``.+-`` internally but
# must *end* in an email-local char (alphanumeric/underscore) immediately
# before ``@``. This drops the decorator/attribute class of false
# positives — ``+@pytest.fixture`` (diff ``+`` as a fake local part),
# ``@app.route``, ``@module.attr`` — where the char before ``@`` is a diff
# marker, whitespace, or absent, while still matching genuine addresses
# whose local part ends in a real char (including a one-char local part).
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_]([a-zA-Z0-9_.+-]*[a-zA-Z0-9_])?@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}",
    re.ASCII,
)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[a-zA-Z0-9_.-]+")
# Every branch must yield a FOUR-octet address. The ``10`` branch carries its own
# second octet because ``10`` alone left it three-wide, so a three-component string
# like a doc section reference or a three-part version string matched as a private
# IP — and a real four-octet address matched only its first three octets. The two
# guards bound the quad against a LONGER dotted run on either side: a preceding dot
# would let a five-part build train donate an inner quad, and a following ``.<digit>``
# the same from the right. A trailing sentence period is not one of them.
_IP_RE = re.compile(r"\b(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b(?!\.\d)")
_API_KEY_RE = re.compile(r"\b(?:glpat-|sk-|ghp_|gho_|github_pat_|xoxb-|xoxp-)[a-zA-Z0-9_-]{10,}")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9-]+\.internal\.[a-z]+\b|\b[a-z0-9-]+\.corp\.[a-z]+\b")
_RESERVED_DOMAIN_RE = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost)$", re.IGNORECASE)

# An SSH git remote — ``git@<host>:<org>/<repo>(.git)`` — is transport
# syntax, never an email/PII. ``_EMAIL_RE`` matches the ``git@<host>``
# prefix, so any test or code carrying a normal SSH remote URL would
# otherwise trip the public-repo privacy gate (a recurring false
# positive). The SSH user is always literally ``git`` and the host is
# followed by ``:<path>`` — a real email never has a ``:path`` after the
# domain — so this is a tight, non-weakening exclusion.
_SSH_GIT_REMOTE_RE = re.compile(r"\bgit@[a-zA-Z0-9.-]+:[A-Za-z0-9._/~-]+")


def _is_reserved_domain(address_or_host: str) -> bool:
    return _RESERVED_DOMAIN_RE.search(address_or_host.rpartition("@")[2]) is not None


def _is_ssh_git_remote(line: str, match: re.Match[str]) -> bool:
    """True when an email match is actually the ``git@host`` of an SSH remote."""
    return any(rm.start() <= match.start() and match.end() <= rm.end() for rm in _SSH_GIT_REMOTE_RE.finditer(line))


def _is_exempt(masked: str, start: int, end: int) -> bool:
    return masked[start:end] == " " * (end - start)


def generic_findings(text: str, *, allowlist: tuple[str, ...] = (), forge_write: bool = False) -> list[GenericFinding]:
    findings: list[GenericFinding] = []
    masked = mask_exempt(text, allowlist) if allowlist else text
    offset = 0
    for line in text.splitlines(keepends=True):
        findings.extend(
            GenericFinding("email", match.group(), offset + match.start())
            for match in _EMAIL_RE.finditer(line)
            if not _is_reserved_domain(match.group())
            and not _is_ssh_git_remote(line, match)
            and not _is_exempt(masked, offset + match.start(), offset + match.end())
        )
        findings.extend(
            GenericFinding("home_path", match.group(), offset + match.start())
            for match in _HOME_PATH_RE.finditer(line)
            if not _is_exempt(masked, offset + match.start(), offset + match.end())
            and not (
                forge_write and line[match.start() :].startswith("/home/runner/work/")  # privacy-scan:allow CI path
            )
        )
        findings.extend(
            GenericFinding("private_ip", match.group(), offset + match.start())
            for match in _IP_RE.finditer(line)
            if not _is_exempt(masked, offset + match.start(), offset + match.end())
        )
        findings.extend(
            GenericFinding("api_key", match.group()[:20] + "...", offset + match.start())
            for match in _API_KEY_RE.finditer(line)
            if not _is_exempt(masked, offset + match.start(), offset + match.end())
        )
        findings.extend(
            GenericFinding("internal_hostname", match.group(), offset + match.start())
            for match in _HOSTNAME_RE.finditer(line)
            if not _is_reserved_domain(match.group())
            and not _is_exempt(masked, offset + match.start(), offset + match.end())
        )
        offset += len(line)
    return findings
