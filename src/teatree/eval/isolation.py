"""Virgin-environment isolation for the in-process Agent-SDK eval runs.

The core harness drives ``claude`` (via ``claude-agent-sdk``) to both produce a
run (:class:`~teatree.eval.sdk_runner.SdkInProcessRunner`) and grade one
(:class:`~teatree.eval.judge.ClaudeJudge`). Without isolation the child
auto-discovers the developer's personal context — ``~/.claude/CLAUDE.md``,
auto-memory, and the project ``CLAUDE.md`` reachable from the parent cwd — and a
scenario can pass because the agent *remembers* a rule rather than because a
teatree gate enforces it. That biases every real eval result.

:func:`isolated_claude_env` yields the ``(env, cwd)`` pair both invoke paths pass
into the SDK options: a copy of the parent environment with ``HOME`` (and the
related config-dir vars) redirected at a freshly created, ``.claude``-free temp
directory, and that same directory as a neutral cwd. The metered ``ANTHROPIC_API_KEY``
and ``PATH`` survive untouched so the SDK backend's auth keeps working; only the
personal-context discovery roots move.

Critically, the child env STRIPS the subscription ``CLAUDE_CODE_OAUTH_TOKEN``: the
metered eval lane authenticates EXCLUSIVELY via ``ANTHROPIC_API_KEY``, and the
bundled CLI prefers the API key only when the OAuth token is NOT also present — so
leaving the OAuth token in the child env would let the SDK bill the subscription
(which a full run throttles). The "which vars conflict with the metered API key"
policy is owned by :class:`~teatree.llm.credentials.AnthropicApiKeyCredential`
(its ``spec.conflicting_vars``), so this helper strips exactly that set rather than
hard-coding the name — one source of truth for the credential policy. The key
itself is exported upstream by ``make_runner`` (the credential's ``export()``), so
it is already present in ``os.environ`` and survives the copy untouched. The
redirect is the belt to the SDK options' suspenders (``setting_sources=[]`` + a
plain-string ``system_prompt`` + empty ``settings``), keeping the run virgin even
if those are loosened.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.llm.credentials import AnthropicApiKeyCredential

_HOME_ANCHORED_VARS = ("HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR")

#: The credentials the metered API key conflicts with — stripped from the child
#: env so the bundled CLI cannot bill the subscription. Owned by the credential
#: layer (one source of truth), not hard-coded here.
_METERED_CONFLICTING_VARS = AnthropicApiKeyCredential().spec.conflicting_vars


@contextmanager
def isolated_claude_env() -> Iterator[tuple[dict[str, str], str]]:
    """Yield ``(env, cwd)`` that runs ``claude`` free of the developer's context.

    ``env`` is the parent environment with the home-anchored discovery roots
    pointed at a private empty directory and every credential that conflicts with
    the metered ``ANTHROPIC_API_KEY`` (the subscription ``CLAUDE_CODE_OAUTH_TOKEN``)
    stripped — so the metered SDK can never bill the subscription. ``cwd`` is that
    directory. The directory is removed when the context exits.
    """
    with tempfile.TemporaryDirectory(prefix="t3-eval-virgin-home-") as home:
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
        env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
        for conflicting in _METERED_CONFLICTING_VARS:
            env.pop(conflicting, None)
        yield env, home


__all__ = ["isolated_claude_env"]
