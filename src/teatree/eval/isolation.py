"""Virgin-environment isolation for the in-process Agent-SDK eval runs.

The core harness drives ``claude`` (via ``claude-agent-sdk``) to both produce a
run (:class:`~teatree.eval.api_runner.ApiInProcessRunner`) and grade one
(:class:`~teatree.eval.judge.ClaudeJudge`). Without isolation the child
auto-discovers the developer's personal context — ``~/.claude/CLAUDE.md``,
auto-memory, and the project ``CLAUDE.md`` reachable from the parent cwd — and a
scenario can pass because the agent *remembers* a rule rather than because a
teatree gate enforces it. That biases every real eval result.

:func:`isolated_claude_env` yields the ``(env, cwd)`` pair both invoke paths pass
into the SDK options: a copy of the parent environment with ``HOME`` (and the
related config-dir vars) redirected at a freshly created, ``.claude``-free temp
directory, and that same directory as a neutral cwd. The SELECTED eval credential
(``CLAUDE_CODE_OAUTH_TOKEN`` for the default subscription lane, ``ANTHROPIC_API_KEY``
for the metered lane) and ``PATH`` survive untouched so the SDK backend's auth keeps
working; only the personal-context discovery roots move.

Critically, the child env STRIPS the *conflicting* credential — the one the
selected eval credential must not fall back to. Which var is stripped is the
caller's decision: it passes ``conflicting_vars`` (the selected credential's
``spec.conflicting_vars``), so the OAuth lane strips ``ANTHROPIC_API_KEY`` and the
metered lane strips ``CLAUDE_CODE_OAUTH_TOKEN``. This is what makes "use THIS eval
credential, exclusively" hold — the bundled CLI prefers a credential only when the
others are absent. The credential itself is exported upstream by ``make_runner`` /
the judge (the credential's ``export()``), so it is already present in ``os.environ``
and survives the copy untouched. The strip set defaults to the metered lane's
conflicts (strip the OAuth token) for a caller that passes none — preserving the
pre-#2707-reversal behaviour for non-eval callers — while the eval chokepoints pass
the resolved eval credential's set. The redirect is the belt to the SDK options'
suspenders (``setting_sources=[]`` + a plain-string ``system_prompt`` + empty
``settings``), keeping the run virgin even if those are loosened.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.llm.credentials import AnthropicApiKeyCredential, CredentialError, base_url_refusal

_HOME_ANCHORED_VARS = ("HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR")

#: The strip set a caller gets when it passes no ``conflicting_vars`` — the metered
#: lane's conflicts (strip the subscription OAuth token). Non-eval callers of
#: :func:`isolated_claude_env` (e.g. ``ticket_short_describe``) keep this default; the
#: eval chokepoints pass the SELECTED eval credential's ``spec.conflicting_vars``.
_DEFAULT_CONFLICTING_VARS = AnthropicApiKeyCredential().spec.conflicting_vars


@contextmanager
def isolated_claude_env(
    conflicting_vars: tuple[str, ...] = _DEFAULT_CONFLICTING_VARS,
    forbidden_vars: tuple[str, ...] = (),
) -> Iterator[tuple[dict[str, str], str]]:
    """Yield ``(env, cwd)`` that runs ``claude`` free of the developer's context.

    ``env`` is the parent environment with the home-anchored discovery roots
    pointed at a private empty directory and every var in *conflicting_vars* (the
    credential the selected eval credential must not fall back to) stripped — so the
    SDK / bundled CLI authenticates with exactly the selected eval credential and
    can never fall back to a conflicting one. ``cwd`` is that directory. The
    directory is removed when the context exits.

    *forbidden_vars* (the selected credential's ``spec.forbidden_vars``) are REFUSED
    rather than stripped: this env is a copy of the parent's, so an ambient
    ``ANTHROPIC_BASE_URL`` would otherwise redirect a subscription-authenticated eval
    child at a third-party endpoint. Raising here matches the dispatch lane's
    behaviour, so both Claude-spawning seams refuse the same combination.
    """
    _reject_forbidden(forbidden_vars)
    with tempfile.TemporaryDirectory(prefix="t3-eval-virgin-home-") as home:
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
        env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
        for conflicting in conflicting_vars:
            env.pop(conflicting, None)
        yield env, home


def _reject_forbidden(forbidden_vars: tuple[str, ...]) -> None:
    """Raise when the ambient env carries a var the selected credential refuses.

    Empty values are treated as absent — an exported-but-blank var expresses no
    redirect. The message names the variable and the remedy so an operator is never
    left guessing which of the two eval lanes refused.
    """
    present = [var for var in forbidden_vars if os.environ.get(var, "").strip()]
    if not present:
        return
    raise CredentialError(
        base_url_refusal(
            present,
            authenticator="the selected eval credential authenticates against the Anthropic subscription",
            remedy="select the metered API key for this eval lane to route it through that endpoint",
        )
    )


__all__ = ["isolated_claude_env"]
