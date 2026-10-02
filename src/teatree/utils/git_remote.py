"""Pure remote-URL parsing/formatting helpers — no git invocation, no I/O.

This is the side-effect-free partition of git-remote handling, split out of
:mod:`teatree.utils.git` (which holds the subprocess-invoking git operations).
Every function here takes a remote-URL string and returns a derived string
(slug or web origin) with no process spawn, so both ``core`` and the management
commands can use them without pulling in the runner machinery or risking a
layering violation. The invoking counterpart — ``remote_url`` /
``remote_slug``, which actually shell out to ``git remote get-url`` — stays in
:mod:`teatree.utils.git`.
"""

import re

_REMOTE_HOST_RE = re.compile(r"^(?:git@[^:]+:|https?://[^/]+/|ssh://[^/]+/|git://[^/]+/)")
_SSH_HOST_RE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/]")
_HTTP_HOST_RE = re.compile(r"^(https?)://([^/]+)/")
#: Any ``<scheme>://[user@]host[:port]/`` form — the host half of every non-ssh URL.
_SCHEME_HOST_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/]+)/", re.IGNORECASE)


def slug_from_remote(remote_url: str) -> str:
    """Extract the ``org/repo`` (or ``ns/group/repo``) slug from a git remote URL.

    Pure string helper (no git invocation). Lives in ``utils`` so both
    ``core`` and the management commands can use it without a layering
    violation.
    """
    if not remote_url:
        return ""
    return _REMOTE_HOST_RE.sub("", remote_url.strip()).removesuffix(".git")


def host_from_remote(remote_url: str) -> str:
    """The network host a git remote URL points at, or ``""`` when it names none.

    Pure string helper, the host counterpart of :func:`slug_from_remote` (which
    deliberately strips the host). Handles ``git@host:slug``, ``ssh://git@host/slug``,
    ``https://host/slug`` and ``git://host/slug``, lower-cased with any ``:port``
    stripped so ``ssh://git@GitHub.com:22/…`` and ``https://github.com/…`` compare
    equal.

    Returns ``""`` for a URL with no network host — a bare filesystem path, a
    relative path, or a ``file://`` URL. That is *unknown*, NOT "a different host":
    a caller comparing hosts must treat the empty answer as non-discriminating
    rather than as a mismatch.
    """
    if not remote_url:
        return ""
    text = remote_url.strip()
    if text.startswith("file://"):
        return ""
    ssh_match = _SSH_HOST_RE.match(text)
    if ssh_match:
        return _normalize_host(ssh_match.group(1))
    scheme_match = _SCHEME_HOST_RE.match(text)
    if scheme_match:
        return _normalize_host(scheme_match.group(1))
    return ""


def _normalize_host(host: str) -> str:
    """Lower-case, drop a trailing numeric ``:port``, and drop a leading ``www.``."""
    bare = host.strip()
    head, separator, tail = bare.rpartition(":")
    if separator and tail.isdigit():
        bare = head
    return bare.lower().removeprefix("www.")


def web_base_from_remote(remote_url: str) -> str:
    """Derive the host web origin (``https://host``) from a git remote URL.

    Handles ``git@host:slug.git``, ``ssh://git@host/slug`` and
    ``https://host/slug`` forms. Returns ``""`` when no host can be parsed.
    """
    if not remote_url:
        return ""
    text = remote_url.strip()
    ssh_match = _SSH_HOST_RE.match(text)
    if ssh_match:
        return f"https://{ssh_match.group(1)}"
    http_match = _HTTP_HOST_RE.match(text)
    if http_match:
        return f"{http_match.group(1)}://{http_match.group(2)}"
    return ""
