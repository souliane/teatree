"""Fitness function: no Slack post can bypass the 90-char wrap (#3809).

The wrap is enforced at two seams — ``SlackBotBackend._post`` for the Django app
and ``hooks.slack_mirror.slack_post_message`` for the hook process. A new sender
that reaches ``chat.postMessage`` from anywhere else inherits nothing, which is
the exact failure the issue asks to make impossible by construction rather than
by remembering. :class:`TestNoBypassingCallSite` turns such a call site red.

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

_POST_METHOD = "chat.postMessage"

#: The only modules that may NAME ``chat.postMessage`` in a call. ``egress``
#: posts solely through the ``_post`` it is handed (the wrap seam), and
#: ``slack_mirror`` wraps in its own body. ``bot`` is absent by construction:
#: its seam dispatches on the *method* parameter, so it never names the string
#: in a call. Anything else reaches Slack unwrapped and is a bypass.
_WRAP_ROUTED_MODULES = frozenset(
    {
        "backends/slack/egress.py",
        "hooks/slack_mirror.py",
    }
)

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


def _posts_chat_message(source: str, *, filename: str = "<mod>") -> bool:
    """Whether *source* passes ``chat.postMessage`` as a call ARGUMENT.

    Argument position, not any string occurrence: a docstring or a scope-map
    literal naming the method is not an egress and must not be flagged.
    """
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [*node.args, *(kw.value for kw in node.keywords)]
        if any(isinstance(a, ast.Constant) and a.value == _POST_METHOD for a in args):
            return True
    return False


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
    def test_every_chat_post_message_is_inside_the_wrap_path(self) -> None:
        posting = {_rel(p) for p in _source_files() if _posts_chat_message(p.read_text(), filename=str(p))}
        assert posting == _WRAP_ROUTED_MODULES


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
        assert _posts_chat_message('poster("chat.postMessage", token=t, json=body, idempotent=False)')

    def test_a_planted_keyword_bypass_is_flagged(self) -> None:
        assert _posts_chat_message('self._post(method="chat.postMessage", payload=p)')

    def test_a_docstring_mention_is_not_flagged(self) -> None:
        assert not _posts_chat_message('"""Wraps every chat.postMessage body."""\nx = 1')

    def test_a_scope_map_literal_is_not_flagged(self) -> None:
        assert not _posts_chat_message('SCOPES = {"chat.postMessage": "chat:write"}')

    def test_an_empty_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="")') == ["''"]

    def test_a_whitespace_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="   ")') == ["'   '"]

    def test_a_computed_reason_is_flagged(self) -> None:
        assert _bad_exemption_reasons("post(text=t, wrap_exempt_reason=why)") == ["why"]

    def test_a_real_reason_is_not_flagged(self) -> None:
        assert _bad_exemption_reasons('post(text=t, wrap_exempt_reason="ascii art")') == []
