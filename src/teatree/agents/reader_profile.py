"""The quarantined reader dispatch profile — no tools, no credentials (#116, Layer 1).

The context firewall's structural first layer: any stage that ingests UNTRUSTED
content runs under the ``directive_reading`` phase, which the ``phase_tools`` SSOT maps
to the EMPTY toolset. Lane A injects the full complement as
``ClaudeAgentOptions.disallowed_tools`` and Lane B filters its toolset to nothing, so
the reader physically cannot read a file, shell out, fetch a URL, write, or spawn a
sub-agent — the primary guarantee that severs leg B (untrusted content) from legs A+C
(private data + external write) of the lethal trifecta.

This module owns the DEFENSE-IN-DEPTH env layer on top of that tool denial.

``reader_child_env`` is a credential ALLOWLIST: from a base env it keeps only the
model-inference credential the LLM needs to run plus a minimal runtime, dropping every
posting/forge/secret variable. Built as allowlist-of-nothing (provably empty of secrets,
not a best-effort filter).

``reader_env_hermetic`` is the ``os.environ`` scrub sibling of
``teatree.utils.git_run.git_env_hermetic``. The claude-agent-sdk transport merges
``{**os.environ, ..., **options.env}`` and CANNOT DELETE a key ``options.env`` omits, so
a secret left in ``os.environ`` would reach the child regardless of the allowlist. This
context manager removes the secret keys from ``os.environ`` for the spawn window and
restores them after — belt (hermetic strip) plus suspenders (allowlist ``options.env``).
"""

import os
import threading

from teatree.core.modelkit.phases import normalize_phase

#: The quarantined reader phase — the ``phase_tools`` SSOT maps it to the empty toolset.
READER_PHASE = "directive_reading"

#: The model-inference credentials the reader's LLM needs to run at all. Kept by the
#: allowlist even though they match the secret-key shape below (never stripped).
_INFERENCE_CREDENTIAL_KEYS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})

#: The minimal non-secret runtime a spawned ``claude`` child needs to start (PATH to
#: find the binary, HOME / config dir for its state, locale, tmp, and the standard
#: process/XDG/node identity vars). None of these is a credential; every key NOT in the
#: allowlist — including unpatterned secrets like ``DATABASE_URL`` / ``SENTRY_DSN`` /
#: ``AWS_ACCESS_KEY_ID`` / ``REDIS_URL`` — is stripped.
_RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
    }
)

#: The complete reader env ALLOWLIST — inference credential + minimal runtime. Every
#: other key is dropped, whatever its name. An ALLOWLIST (not a denylist) is the whole
#: point: a denylist of known secret shapes leaks any secret it did not anticipate
#: (``DATABASE_URL``, ``SENTRY_DSN``, a custom ``FOO_URL`` holding a token); an allowlist
#: leaks nothing by construction. Both the child-env filter and the ``os.environ`` scrub
#: below share this one set so belt == suspenders.
_READER_ENV_ALLOWLIST: frozenset[str] = _INFERENCE_CREDENTIAL_KEYS | _RUNTIME_KEYS


def is_reader_phase(phase: str) -> bool:
    """Whether *phase* is the quarantined reader phase (spelling-tolerant)."""
    return normalize_phase(phase) == READER_PHASE


def reader_child_env(base: "dict[str, str]") -> dict[str, str]:
    """Return *base* filtered to the reader allowlist — inference credential + runtime only.

    Every non-allowlisted variable is dropped; only :data:`_READER_ENV_ALLOWLIST` keys
    survive. *base* is never mutated. The reader cannot exfiltrate through a credential
    it does not carry (defense-in-depth on top of the tool denial).
    """
    return {key: value for key, value in base.items() if key in _READER_ENV_ALLOWLIST}


class _NestedEnvScrub:
    """Refcounted ``os.environ`` scrub: concurrent readers share one strip, the last out restores.

    ``os.environ`` is process-global, so an unsynchronized strip/restore pair loses the
    guarantee under a multithreaded worker: two overlapping readers each capture their own
    "stripped" mapping, and the first to exit restores every secret while the second is
    still spawning. Holding the depth and the saved mapping behind one lock makes the
    reduced environment span the UNION of the active reader windows.
    """

    def __init__(self, keep: frozenset[str]) -> None:
        self._keep = keep
        self._lock = threading.Lock()
        self._depth = 0
        self._stripped: dict[str, str] = {}

    def __enter__(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._stripped = {k: v for k, v in os.environ.items() if k not in self._keep}
                for key in self._stripped:
                    del os.environ[key]
            self._depth += 1

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth == 0:
                os.environ.update(self._stripped)
                self._stripped = {}


_READER_ENV_SCRUB = _NestedEnvScrub(_READER_ENV_ALLOWLIST)


def reader_env_hermetic() -> _NestedEnvScrub:
    """Strip every non-allowlisted key from ``os.environ`` for the duration, restoring after.

    The mutating sibling of :func:`reader_child_env` — allowlist parity, so belt ==
    suspenders — for the SDK transport that merges ``os.environ`` under ``options.env``
    and cannot DELETE a key the overrides omit. A reader spawned inside this context
    inherits an ``os.environ`` reduced to the allowlist: no posting/forge/secret
    credential of ANY name (a denylist would miss ``DATABASE_URL`` / ``SENTRY_DSN``).
    Keys are restored once the LAST active reader leaves, so the rest of the process is
    unaffected and an overlapping reader never sees them come back early.
    """
    return _READER_ENV_SCRUB
