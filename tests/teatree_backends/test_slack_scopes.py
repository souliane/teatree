"""Tests for ``teatree.backends.slack.scopes`` (X-OAuth-Scopes header parsing)."""

from teatree.backends.slack.scopes import parse_oauth_scopes


def test_parse_oauth_scopes_splits_comma_separated_header() -> None:
    assert parse_oauth_scopes("chat:write,reactions:write, users:read ") == frozenset(
        {"chat:write", "reactions:write", "users:read"}
    )


def test_parse_oauth_scopes_empty_header_yields_empty_set() -> None:
    assert parse_oauth_scopes("") == frozenset()


def test_parse_oauth_scopes_drops_blank_segments() -> None:
    assert parse_oauth_scopes(" , chat:write ,, ") == frozenset({"chat:write"})
