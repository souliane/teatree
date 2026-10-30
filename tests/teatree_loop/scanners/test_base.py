"""Shared scanner-support primitives in ``teatree.loop.scanners.base``.

``hours_since`` is the one cadence-gate helper the once-per-N-hours scanners
(``backlog_sweep``, ``eval_local``, ``provision_smoke``, ``architectural_review``,
``scanning_news``) share, replacing five copy-pasted
``(now - last_run_at).total_seconds() / 3600.0  # type: ignore[operator]`` sites.
Pure clock arithmetic — a unit test on the primitive; each scanner's DB-backed
cadence behaviour (including its never-run ``bootstrap`` branch) is pinned in its
own ``tests/teatree_loop/test_*_scanner.py``.
"""

import datetime as dt
from unittest.mock import Mock, patch

import pytest

from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.scanners.base import hours_since, resolve_identities, run_gh_for_slug


class TestHoursSince:
    def test_whole_hours_elapsed(self) -> None:
        now = dt.datetime(2026, 6, 16, 12, 0, tzinfo=dt.UTC)
        earlier = now - dt.timedelta(hours=5)
        assert hours_since(earlier, now=now) == pytest.approx(5.0)

    def test_fractional_hours_elapsed(self) -> None:
        now = dt.datetime(2026, 6, 16, 12, 0, tzinfo=dt.UTC)
        earlier = now - dt.timedelta(hours=1, minutes=30)
        assert hours_since(earlier, now=now) == pytest.approx(1.5)

    def test_zero_when_earlier_is_now(self) -> None:
        now = dt.datetime(2026, 6, 16, 12, 0, tzinfo=dt.UTC)
        assert hours_since(now, now=now) == pytest.approx(0.0)


class TestResolveIdentities:
    def test_configured_aliases_are_deduplicated_in_first_seen_order(self) -> None:
        host = Mock()
        assert resolve_identities(host, ("alice", "bob", "alice")) == ("alice", "bob")

    def test_configured_aliases_win_over_the_host_user(self) -> None:
        host = Mock()
        host.current_user.return_value = "token-owner"

        assert resolve_identities(host, ("alice",)) == ("alice",)
        host.current_user.assert_not_called()

    def test_no_aliases_falls_back_to_the_host_user(self) -> None:
        host = Mock()
        host.current_user.return_value = "token-owner"
        assert resolve_identities(host, ()) == ("token-owner",)

    def test_no_aliases_and_an_empty_host_user_resolves_to_no_identity(self) -> None:
        host = Mock()
        host.current_user.return_value = ""
        assert resolve_identities(host, ()) == ()


def test_slug_routing_rejects_missing_credentials_without_ambient_gh() -> None:
    resolution = ForgeTokenResolution("github_token", "", ForgeTokenState.UNSET)
    with patch("teatree.forge_credentials.resolve_slug_token", return_value=resolution) as resolve:
        result = run_gh_for_slug(["api", "repos/example/repo"], "example/repo")

    resolve.assert_called_once_with("example/repo", forge="github", credential="github_token")
    assert result.returncode == 4
    assert "ambient gh authentication" in result.stderr
