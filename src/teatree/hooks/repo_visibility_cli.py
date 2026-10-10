"""Shell seam for resolving a git remote's repo visibility.

The shell pre-push gates cannot import Python, so they used to shell
``gh repo view`` directly. That hard-codes ONE forge: a ``gitlab.com`` remote
can never resolve through ``gh`` (it errors on the namespace), so the gate's
visibility came back undetermined on every push and it fell into its
fail-closed branch -- scanning a private repo forever.

This CLI is the thin seam onto :mod:`teatree.hooks._repo_visibility`, which
already routes the probe by the remote's host segment (``gh`` for GitHub,
``glab`` for GitLab) and day-caches the verdict per slug, so a repeat push
costs no network call at all. It is deliberately Django-free -- the import
chain is ``cold_reader`` / ``git_config_offline`` / ``utils.run`` only -- so a
pre-push hook pays an interpreter start, not a framework boot.

Usage::

    python -m teatree.hooks.repo_visibility_cli [--live] <remote-url-or-slug>

Prints exactly one line -- ``PUBLIC`` / ``PRIVATE`` / ``INTERNAL`` /
``UNKNOWN`` -- and exits 0 whenever it could run. ``UNKNOWN`` is the fail-safe
verdict: callers must treat it as "NOT confirmed private" and keep enforcing,
so an absent forge CLI, an unparsable remote, or a probe error never silently
skips a leak scan. It is equally NOT a confirmation of publicness -- a caller
whose rule needs a public remote must require an affirmative ``PUBLIC``.

A reachable forge answer PUBLIC takes precedence over ``private_repos``. If the
probe cannot answer, a matching host-qualified declaration resolves PRIVATE.

``--live`` is the push path's question: ONLY a probe made now may say a repository is
not public. The day cache and the ``private_repos`` allowlist are files a pusher can
write (the cache directly, the allowlist in a store ``T3_CONFIG_DB`` or
``T3_DATA_DIR`` points anywhere), so a PRIVATE they hold would skip a leak scan on
nothing but the pusher's word. Neither is read, and the cache is not written; an
unanswered probe is ``UNKNOWN``, which the gate reads as "scan".

An allowlist that could not be READ is not an empty one. When the probe cannot
decide either, the verdict is ``ALLOWLIST_UNREADABLE`` and the reason goes to
stderr: enforced exactly like ``UNKNOWN``, but named apart, because the remedy is
the store (locked, corrupt, or not where this process looks), never declaring a
repo that is already declared.
"""

import sys
from dataclasses import dataclass

from teatree.config import cold_reader
from teatree.hooks._private_repo_entries import private_repo_entry_matches, warn_malformed_private_entry
from teatree.hooks._repo_visibility import probe_visibility, slug_for_remote_url, slug_visibility

UNKNOWN_VERDICT = "UNKNOWN"
PRIVATE_VERDICT = "PRIVATE"
ALLOWLIST_UNREADABLE_VERDICT = "ALLOWLIST_UNREADABLE"
_ALLOWLIST_KEY = "private_repos"
_FORGE_VERDICTS = frozenset({"PUBLIC", "PRIVATE", "INTERNAL"})


@dataclass(frozen=True, slots=True)
class _Allowlist:
    """The ``private_repos`` entries, or why the store holding them could not be read."""

    entries: tuple[str, ...]
    unreadable: str = ""


@dataclass(frozen=True, slots=True)
class _Resolution:
    verdict: str
    reason: str = ""


def _read_allowlist() -> _Allowlist:
    # ONE confirmed read: a second "was it readable?" probe can succeed under the same
    # lock contention that failed the first and hide it (cold_reader, #4008).
    read = cold_reader.read_setting_confirmed(_ALLOWLIST_KEY)
    db = cold_reader.canonical_config_db()
    if not read.readable:
        return _Allowlist(
            (), f"the config DB at {db} could not be read (locked by a writer, corrupt, or not a teatree DB)"
        )
    if read.value is None:
        if not db.exists() and cold_reader.canonical_projection() is None:
            return _Allowlist((), f"there is no config DB at {db} and no published host projection")
        return _Allowlist(())
    if not isinstance(read.value, list):
        return _Allowlist((), f"the value stored in {db} is a {type(read.value).__name__}, not a list")
    from teatree.config.setting_parsers import _parse_private_repos  # noqa: PLC0415 — cold import

    entries = []
    for entry in read.value:
        try:
            entries.extend(_parse_private_repos([entry]))
        except (TypeError, ValueError):
            warn_malformed_private_entry(entry, db)
    return _Allowlist(tuple(entries))


def _resolve_live(slug: str) -> _Resolution:
    verdict = probe_visibility(slug)
    return _Resolution(verdict if verdict in _FORGE_VERDICTS else UNKNOWN_VERDICT)


def _resolve_declared(slug: str) -> _Resolution:
    allowlist = _read_allowlist()
    verdict = slug_visibility(slug)
    if verdict == "PUBLIC":
        return _Resolution(verdict)
    if verdict in {"PRIVATE", "INTERNAL"}:
        return _Resolution(verdict)
    if any(private_repo_entry_matches(entry, slug, normalize=slug_for_remote_url) for entry in allowlist.entries):
        return _Resolution(PRIVATE_VERDICT)
    if allowlist.unreadable:
        return _Resolution(ALLOWLIST_UNREADABLE_VERDICT, f"could not read {_ALLOWLIST_KEY}: {allowlist.unreadable}")
    return _Resolution(UNKNOWN_VERDICT)


def _resolve(url: str, *, live: bool = False) -> _Resolution:
    slug = slug_for_remote_url(url.strip())
    if not slug:
        return _Resolution(UNKNOWN_VERDICT)
    return _resolve_live(slug) if live else _resolve_declared(slug)


def visibility_for_remote(url: str, *, live: bool = False) -> str:
    """Return ``url``'s visibility verdict, or ``UNKNOWN_VERDICT``.

    The remote is normalized to a HOST-QUALIFIED slug first
    (:func:`_repo_visibility.slug_for_remote_url`) so the host-keyed probe
    routes to the forge the remote actually lives on. A host-stripped slug
    would default every remote to the GitHub probe.

    A reachable PUBLIC answer wins over a host-qualified declaration. When
    the forge cannot answer, a matching declaration yields PRIVATE. With *live*
    only the forge's answer, asked now, counts (see the module docstring).
    """
    return _resolve(url, live=live).verdict


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    live = "--live" in args
    args = [arg for arg in args if arg != "--live"]
    if len(args) != 1 or not args[0].strip():
        sys.stdout.write(f"{UNKNOWN_VERDICT}\n")
        return 0
    resolution = _resolve(args[0], live=live)
    if resolution.reason:
        sys.stderr.write(f"{resolution.reason}\n")
    sys.stdout.write(f"{resolution.verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
