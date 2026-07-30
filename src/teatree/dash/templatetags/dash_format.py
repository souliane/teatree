"""Dashboard text filters — the owner's standing formatting directive, in HTML (#3624).

Code symbols and file paths render monospace on every surface the factory writes
to. The Slack digest backticks them; here they become ``<code>``. Detection is the
shared :func:`~teatree.core.code_tokens.rewrite_code_tokens`, so the two surfaces
cannot disagree about what a code token is.
"""

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from teatree.core.code_tokens import rewrite_code_tokens

register = template.Library()


@register.filter
def code_spans(text: str) -> SafeString:
    """Escape *text*, then wrap each file path / dotted symbol in ``<code>``.

    Escaping happens FIRST and the only markup introduced afterwards is the
    ``<code>`` pair this filter emits, so ``mark_safe`` is sound: no caller-supplied
    character survives unescaped.
    """
    escaped = escape(text or "")
    return mark_safe(rewrite_code_tokens(escaped, lambda token: f"<code>{token}</code>"))  # noqa: S308 — input escaped above; only this filter's own <code> markup is added
