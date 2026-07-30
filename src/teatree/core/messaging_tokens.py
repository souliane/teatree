"""Single site resolving the two ASYMMETRIC messaging token-ref conventions (#3334).

``slack_token_ref`` is a PREFIX: the bot and app tokens live at ``<ref>-bot`` /
``<ref>-app``. ``user_token_ref`` is a FULL PATH: read verbatim. The two fields
are named identically (``*_token_ref``) but resolve by different rules — an
asymmetry that carried no signal and was re-derived (and mis-derived) per
consumer, degrading silently to an empty token when guessed wrong. This module
encodes the rule ONCE so no call site guesses.

Every read routes through the send-proxy credential reader
(:func:`teatree.core.send_proxy.read_posting_credential`, #117), so the
single-credential-reader gate stays green and no posting token is read outside
the proxy.
"""

from dataclasses import dataclass


def _read(ref: str) -> str:
    from teatree.core.send_proxy import read_posting_credential  # noqa: PLC0415 — deferred: ORM model, pre-app-registry

    return read_posting_credential(ref)


@dataclass(frozen=True, slots=True)
class ResolvedMessagingTokens:
    """The bot / app / user tokens resolved from the two ref conventions."""

    bot: str
    app: str
    user: str


def resolve_messaging_tokens(
    *,
    slack_token_ref: str,
    user_token_ref: str,
    bot_fallback: str = "",
) -> ResolvedMessagingTokens:
    """Resolve the Slack bot/app/user tokens, encoding the prefix/full-path split.

    ``slack_token_ref`` is a PREFIX — the bot lives at ``<ref>-bot`` and the app
    token at ``<ref>-app``. When it is unset, the bot falls back to *bot_fallback*
    (the overlay's ``get_slack_token()``), mirroring the historical construction
    path. ``user_token_ref`` is a FULL PATH, read verbatim.
    """
    bot = _read(f"{slack_token_ref}-bot") if slack_token_ref else bot_fallback
    app = _read(f"{slack_token_ref}-app") if slack_token_ref else ""
    user = _read(user_token_ref)
    return ResolvedMessagingTokens(bot=bot, app=app, user=user)


def diagnose_configured_ref(field: str, ref: str, *, suffix: str = "") -> str | None:
    """Diagnose a CONFIGURED-but-unresolvable messaging token ref; ``None`` when fine.

    An unset ref (``""``) is a legitimate no-op → ``None``. A set ref that reads
    back empty from the store is always a bug (#3334): the returned message names
    the field AND the exact ``pass`` entry that came back empty, converting the
    silent-degradation class into a diagnosed one. ``suffix`` handles the PREFIX
    convention (``slack_token_ref`` → probe ``<ref>-bot``); ``user_token_ref``
    passes no suffix, since it is a full path.
    """
    if not ref:
        return None
    entry = f"{ref}{suffix}"
    if _read(entry):
        return None
    return (
        f"{field}={ref!r} is configured but its `pass` entry {entry!r} reads back empty — "
        f"messaging silently degrades. Fix it (`pass insert {entry}`) or clear {field}."
    )


__all__ = ["ResolvedMessagingTokens", "diagnose_configured_ref", "resolve_messaging_tokens"]
