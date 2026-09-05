"""Fitness function: no text-bearing Slack egress bypasses the 90-char wrap (#3809).

Three Slack wire calls carry a message body, and each has its own wrap seam:
``chat.postMessage`` (``SlackBotBackend._post`` in the Django app,
``hooks.slack_mirror.slack_post_message`` in the hook process),
``files.completeUploadExternal``'s ``initial_comment`` (the audio DM, which
never touches ``chat.postMessage`` at all), and the incoming webhook, which
posts raw over ``httpx`` outside the backend transport entirely.

A new sender that reaches any of them from elsewhere inherits nothing, which is
the exact failure the issue asks to make impossible by construction rather than
by remembering. :class:`TestNoBypassingCallSite` turns such a call site red, and
:class:`TestNoUnaccountedRawHttpEgress` covers the webhook shape, which names no
Slack method for the first checker to key on.

Block Kit ``blocks`` stay outside the guarantee by design — Block Kit owns its
own layout, and only the ``text`` fallback is wrapped.

The second half guards the escape hatch. ``wrap_exempt_reason`` is deliberately
reviewable — a reason string is visible in a diff where a bare bool is not — so
:class:`TestExemptionsCarryAReason` refuses an empty or non-literal reason, which
would be an implicit escape that quietly disables the rule.

Both halves carry an anti-vacuity control: a green here is only meaningful if the
checker is shown to flag the violation it looks for.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "teatree"

#: Every Slack API method whose payload carries a message body.
_TEXT_BEARING_METHODS = frozenset({"chat.postMessage", "files.completeUploadExternal"})

#: The only modules that may NAME one of those methods in a call. ``egress``
#: posts solely through the ``_post`` it is handed (the wrap seam), while
#: ``slack_mirror`` and ``audio_upload`` wrap in their own bodies. ``bot`` is
#: absent by construction: its seam dispatches on the *method* parameter, so it
#: never names the string in a call. Anything else reaches Slack unwrapped.
_WRAP_ROUTED_MODULES = frozenset(
    {
        "backends/slack/audio_upload.py",
        "backends/slack/egress.py",
        "hooks/slack_mirror.py",
    }
)

#: Slack-backend modules that call ``httpx`` directly rather than through
#: ``SlackHttpClient``, each with why it cannot emit an unwrapped body.
_RAW_HTTP_MODULES = {
    "backends/slack/client.py": "post_webhook_message wraps in its own body",
    "backends/slack/http.py": "the shared transport; bodies are wrapped before they reach it",
    "backends/slack/reactions.py": "reactions.add / reactions.get carry no text",
    "backends/slack/socket_mode.py": "apps.connections.open carries no text",
}

#: Modules that DEFINE and forward the ``wrap_exempt_reason`` parameter. They
#: pass it as a variable because they are plumbing, not callers opting out, so
#: the non-empty-literal rule below cannot apply to them.
_PLUMBING_MODULES = frozenset(
    {
        "backends/slack/bot.py",
        "backends/slack/egress.py",
    }
)


def _rel(path: Path) -> str:
    return path.relative_to(_SRC).as_posix()


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _posts_message_text(source: str, *, filename: str = "<mod>") -> bool:
    """Whether *source* passes a text-bearing Slack method as a call ARGUMENT.

    Argument position, not any string occurrence: a docstring or a scope-map
    literal naming the method is not an egress and must not be flagged.
    """
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [*node.args, *(kw.value for kw in node.keywords)]
        if any(isinstance(a, ast.Constant) and a.value in _TEXT_BEARING_METHODS for a in args):
            return True
    return False


def _calls_httpx_directly(source: str, *, filename: str = "<mod>") -> bool:
    """Whether *source* issues an ``httpx.<verb>(...)`` call of its own."""
    tree = ast.parse(source, filename=filename)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        for node in ast.walk(tree)
    )


def _bad_exemption_reasons(source: str, *, filename: str = "<mod>") -> list[str]:
    """Every ``wrap_exempt_reason`` that is not a non-empty string literal.

    A computed reason cannot be reviewed by reading the diff, so it is refused
    alongside an empty one.
    """
    tree = ast.parse(source, filename=filename)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "wrap_exempt_reason":
                continue
            value = kw.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value.strip():
                bad.append(ast.unparse(value))
    return bad


class TestNoBypassingCallSite:
    def test_every_text_bearing_method_is_inside_the_wrap_path(self) -> None:
        posting = {_rel(p) for p in _source_files() if _posts_message_text(p.read_text(), filename=str(p))}
        assert posting == _WRAP_ROUTED_MODULES


class TestNoUnaccountedRawHttpEgress:
    """The webhook names no Slack method, so the checker above cannot see it."""

    def test_every_raw_httpx_call_site_in_the_slack_backend_is_accounted_for(self) -> None:
        slack_backend = _SRC / "backends" / "slack"
        raw = {
            _rel(p)
            for p in slack_backend.rglob("*.py")
            if "__pycache__" not in p.parts and _calls_httpx_directly(p.read_text(), filename=str(p))
        }
        assert raw == set(_RAW_HTTP_MODULES)


class TestExemptionsCarryAReason:
    def test_no_caller_opts_out_with_a_blank_or_computed_reason(self) -> None:
        offenders = {
            _rel(p): bad
            for p in _source_files()
            if _rel(p) not in _PLUMBING_MODULES and (bad := _bad_exemption_reasons(p.read_text(), filename=str(p)))
        }
        assert offenders == {}


class TestCheckerIsAntiVacuous:
    """The live-tree greens above are meaningless if these do not fire."""

    def test_a_planted_bypassing_call_site_is_flagged(self) -> None:
        assert _posts_message_text('poster("chat.postMessage", token=t, json=body, idempotent=False)')

    def test_a_planted_upload_bypass_is_flagged(self) -> None:
        assert _posts_message_text('http.post("files.completeUploadExternal", json=payload)')

    def test_a_planted_keyword_bypass_is_flagged(self) -> None:
        assert _posts_message_text('self._post(method="chat.postMessage", payload=p)')

    def test_a_docstring_mention_is_not_flagged(self) -> None:
        assert not _posts_message_text('"""Wraps every chat.postMessage body."""\nx = 1')

    def test_a_scope_map_literal_is_not_flagged(self) -> None:
        assert not _posts_message_text('SCOPES = {"chat.postMessage": "chat:write"}')

    def test_a_planted_raw_httpx_egress_is_flagged(self) -> None:
        assert _calls_httpx_directly('httpx.post(url, json={"text": body})')

    def test_a_client_attribute_call_is_not_flagged(self) -> None:
        assert not _calls_httpx_directly("self._http.post(method, json=payload)")

    def test_an_empty_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="")') == ["''"]

    def test_a_whitespace_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="   ")') == ["'   '"]

    def test_a_computed_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons("post(text=t, wrap_exempt_reason=why)") == ["why"]

    def test_a_real_reason_is_not_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="ascii art")') == []
