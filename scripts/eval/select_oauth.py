r"""Point the eval run's ``CLAUDE_CODE_OAUTH_TOKEN`` at the FRESHEST candidate account.

The metered behavioral-eval lanes authenticate with ONE subscription OAuth token. When
the ``EVAL_OAUTH_TOKENS`` CI secret holds several accounts (newline-separated), a run
must never land on a throttled/exhausted one — so this pre-eval step probes each
account's remaining usage-window headroom and exports the freshest token for the eval
step that follows.

THE PROBE + SCORE live in :mod:`teatree.eval.oauth_selection`: one tiny
``POST /v1/messages`` per token reads the ``anthropic-ratelimit-unified-*`` response
headers (a 429 still carries them), and the accounts rank on binding-then-weighted
window headroom, deterministic first on a tie. Runs in seconds; imports only the
foundation-pure probe, so it needs no ``django.setup()``.

THIS STEP OWNS THE CREDENTIAL DECISION. Whenever it makes one it exports
``T3_AGENT_HARNESS_PROVIDER`` and (on the OAuth path) ``CLAUDE_CODE_OAUTH_TOKEN`` into
``$GITHUB_ENV`` — the eval step reads both from there rather than pinning them in its own
``env:`` block, so this step's dynamic choice is authoritative. With nothing configured at
all there is no decision to record: ``subscription_oauth`` is already the absent-setting
default (:class:`~teatree.config.agent_enums.AgentHarnessProvider`), so the clean no-op
below leaves the eval exactly where an unexported provider would.

*   ``EVAL_CREDENTIAL`` is not ``subscription_oauth`` (e.g. an ``api_key`` run) → export
    the provider and stop; there is no OAuth account to select.
*   ``EVAL_OAUTH_TOKENS`` unset/empty → BACKWARD-SAFE passthrough: export the existing
    single ``CLAUDE_CODE_OAUTH_TOKEN`` secret unchanged, so behaviour is exactly as
    before this step existed.
*   A freshest account is found → export it as ``CLAUDE_CODE_OAUTH_TOKEN``.
*   EVERY candidate is exhausted/unreachable → with ``EVAL_API_KEY_FALLBACK`` truthy
    (opt-in, DEFAULT OFF) flip the provider to ``api_key`` so the eval rides the metered
    ``ANTHROPIC_API_KEY`` instead; otherwise exit non-zero (fail loud), since a run on a
    throttled account produces garbage $0.00 shards — worse than a red pre-check.

SECRET HYGIENE. A token value is NEVER printed as a plain log line. The winning token
is written straight to the ``$GITHUB_ENV`` file and registered with GitHub's
``::add-mask::`` directive (the one line where a token value is emitted — the masking
command itself, which GitHub redacts from all subsequent output). Candidates are
identified by position (``token[1]``, ``token[2]``, …); the losing tokens are never
emitted anywhere.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from teatree.eval.oauth_selection import parse_tokens, select_freshest

__all__ = ["main"]

PROVIDER_ENV = "T3_AGENT_HARNESS_PROVIDER"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — an env var NAME, not a secret value
SUBSCRIPTION_OAUTH = "subscription_oauth"
API_KEY = "api_key"

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _mask(token: str) -> None:
    """Register *token* with GitHub's log masker.

    ``::add-mask::VALUE`` is the sanctioned way to hide a value: GitHub intercepts the
    directive and redacts VALUE from every subsequent log line. This is the ONLY place a
    token value is emitted, and it is the masking command itself — never a plain log.
    """
    print(f"::add-mask::{token}")


def _export(env_file: str | None, assignments: Mapping[str, str]) -> None:
    """Append ``KEY=VALUE`` lines to the ``$GITHUB_ENV`` file (a no-op with no file)."""
    if not env_file:
        return
    with Path(env_file).open("a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in assignments.items())


def main(env: Mapping[str, str] | None = None) -> int:
    environ = env if env is not None else os.environ
    github_env = environ.get("GITHUB_ENV")
    provider = (environ.get("EVAL_CREDENTIAL") or "").strip() or SUBSCRIPTION_OAUTH

    if provider != SUBSCRIPTION_OAUTH:
        _export(github_env, {PROVIDER_ENV: provider})
        print(f"credential={provider} → no OAuth selection needed.")
        return 0

    tokens = parse_tokens(environ.get("EVAL_OAUTH_TOKENS", ""))
    if not tokens:
        fallback = (environ.get(OAUTH_TOKEN_ENV) or "").strip()
        if fallback:
            _mask(fallback)
            _export(github_env, {PROVIDER_ENV: SUBSCRIPTION_OAUTH, OAUTH_TOKEN_ENV: fallback})
            print("EVAL_OAUTH_TOKENS unset/empty → using the existing CLAUDE_CODE_OAUTH_TOKEN secret unchanged.")
            return 0
        print("EVAL_OAUTH_TOKENS unset/empty and no CLAUDE_CODE_OAUTH_TOKEN secret set — nothing to select.")
        return 0

    selection = select_freshest(tokens)
    winner = selection.winner
    if winner is not None:
        token = tokens[winner.index]
        _mask(token)
        _export(github_env, {PROVIDER_ENV: SUBSCRIPTION_OAUTH, OAUTH_TOKEN_ENV: token})
        print(
            f"selected {winner.label} (org {winner.organization_id or 'unknown'}) — "
            f"binding headroom {winner.binding_headroom * 100:.0f}%, "
            f"5h {winner.headroom_5h * 100:.0f}% / weekly {winner.headroom_7d * 100:.0f}% free."
        )
        return 0

    print("no eligible OAuth account — every candidate is exhausted or unreachable:")
    for candidate in selection.candidates:
        print(f"  {candidate.label}: {candidate.reason or candidate.status.value}")
    if _is_truthy(environ.get("EVAL_API_KEY_FALLBACK")):
        _export(github_env, {PROVIDER_ENV: API_KEY})
        print("EVAL_API_KEY_FALLBACK is set → falling back to the metered ANTHROPIC_API_KEY (api_key).")
        return 0
    print("EVAL_API_KEY_FALLBACK is off → failing loud (a run on a throttled account is worse than a red pre-check).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
