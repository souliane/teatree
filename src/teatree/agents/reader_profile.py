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

import contextlib
import os
from collections.abc import Iterator

from teatree.core.modelkit.phases import normalize_phase

#: The quarantined reader phase — the ``phase_tools`` SSOT maps it to the empty toolset.
READER_PHASE = "directive_reading"

#: The model-inference credentials the reader's LLM needs to run at all. Kept by the
#: allowlist even though they match the secret-key shape below (never stripped).
_INFERENCE_CREDENTIAL_KEYS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})

#: The minimal non-secret runtime a spawned ``claude`` child needs (PATH to find the
#: binary, HOME / config dir for its state, locale + tmp). No posting/forge credential.
_RUNTIME_KEYS: frozenset[str] = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TMP", "CLAUDE_CONFIG_DIR"}
)

#: The complete reader env allowlist — inference credential + minimal runtime. Every
#: other key (Slack/forge tokens, secret resolvers, arbitrary ``*_TOKEN``/``*_KEY``) is
#: dropped. This is the whole no-creds guarantee.
_READER_ENV_ALLOWLIST: frozenset[str] = _INFERENCE_CREDENTIAL_KEYS | _RUNTIME_KEYS

#: Env-key shapes that name a posting/forge/secret credential — stripped from
#: ``os.environ`` for the reader's spawn window. The inference credential is exempt
#: (it is in the allowlist) so the reader can still authenticate to the model.
_SECRET_KEY_PREFIXES: tuple[str, ...] = ("SLACK_", "GH_", "GITHUB_", "GITLAB_", "PASS", "T3_SECRET", "NOTION_")
_SECRET_KEY_SUFFIXES: tuple[str, ...] = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")


def is_reader_phase(phase: str) -> bool:
    """Whether *phase* is the quarantined reader phase (spelling-tolerant)."""
    return normalize_phase(phase) == READER_PHASE


def reader_child_env(base: "dict[str, str]") -> dict[str, str]:
    """Return *base* filtered to the reader allowlist — inference credential + runtime only.

    Every posting/forge/secret variable is dropped; only :data:`_READER_ENV_ALLOWLIST`
    keys survive. *base* is never mutated. The reader cannot exfiltrate through a
    credential it does not carry (defense-in-depth on top of the tool denial).
    """
    return {key: value for key, value in base.items() if key in _READER_ENV_ALLOWLIST}


def _is_secret_key(key: str) -> bool:
    """Whether *key* names a secret to strip from ``os.environ`` for the spawn window.

    The inference credential is never a secret to strip (the reader needs it); every
    other ``SLACK_*`` / ``*_TOKEN`` / ``*_KEY`` / ``*_SECRET`` / secret-resolver key is.
    """
    if key in _READER_ENV_ALLOWLIST:
        return False
    if any(key.startswith(prefix) for prefix in _SECRET_KEY_PREFIXES):
        return True
    return any(key.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


@contextlib.contextmanager
def reader_env_hermetic() -> Iterator[None]:
    """Strip every secret key from ``os.environ`` for the duration, restoring after.

    The mutating sibling of :func:`reader_child_env`, for the SDK transport that merges
    ``os.environ`` under ``options.env`` and cannot DELETE a key the overrides omit. A
    reader spawned inside this context inherits an ``os.environ`` with no
    posting/forge/secret credential — the only point such a child is guaranteed
    credential-free. Keys are restored on exit so the rest of the process is unaffected.
    """
    stripped = {key: value for key, value in os.environ.items() if _is_secret_key(key)}
    for key in stripped:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(stripped)
