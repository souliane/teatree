"""A slow ``gh``/``glab`` visibility probe must fail SAFE, never propagate.

:mod:`teatree.hooks._repo_visibility` runs each ``gh repo view`` / ``glab api``
probe under a tight ``timeout`` so a hung forge call cannot block the caller.
:func:`probe_visibility` documents a ``None`` (fail-safe "unknown") result on
ANY probe error, and the git-remote resolver documents a ``""`` fail-safe. The
probe subprocess raises :class:`subprocess.TimeoutExpired` on timeout, which is
NOT a subclass of ``OSError``/``CommandFailedError`` — before this fix it escaped
the ``except`` clauses and propagated.

That escape is the root cause of the shuffled-collection CI red on
``tests/teatree_loop/test_slack_broadcasts_own_author_identity.py``: the
broadcast scanner's own-author skip calls
:func:`teatree.core.review.author_trust.classify_author`, which probes repo
visibility. A timed-out probe raised through ``classify_author`` into the
scanner's broad ``except``, which logged "failed on message" and dropped the
review-intent signal — so the colleague-MR broadcast dispatched nothing.
"""

import time
from pathlib import Path

import pytest

from teatree.core.review.author_trust import classify_author
from teatree.hooks import _repo_visibility
from teatree.utils.run import TimeoutExpired


def _raise_timeout(cmd: object, *_args: object, **kwargs: object) -> object:
    raise TimeoutExpired(cmd, kwargs.get("timeout"))


@pytest.fixture(autouse=True)
def _resolve_fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the probe find ``gh``/``glab``/``git`` so it reaches the subprocess call."""
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda tool: f"/usr/bin/{tool}")


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the visibility verdict cache under ``tmp_path`` so no on-disk verdict masks the probe."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))


class TestProbeTimeoutFailsSafe:
    """A timed-out probe returns the documented fail-safe verdict, never raises."""

    def test_github_probe_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        assert _repo_visibility.probe_visibility("github.com/octo/repo") is None

    def test_gitlab_probe_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        assert _repo_visibility.probe_visibility("gitlab.com/team/project") is None

    def test_slug_is_private_timeout_is_not_private(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        # An unresolvable (timed-out) probe must treat the repo as NOT private, not raise.
        assert _repo_visibility.slug_is_private("github.com/octo/repo") is False


class TestNegativeVisibilityCaching:
    """An unresolved probe is short-TTL cached so it is not re-probed every publish."""

    def test_unresolved_verdict_is_cached_and_not_reprobed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _probe(_slug: str) -> str | None:
            calls["n"] += 1
            return None

        monkeypatch.setattr(_repo_visibility, "probe_visibility", _probe)
        assert _repo_visibility.slug_visibility("github.com/octo/mystery") is None
        assert _repo_visibility.slug_visibility("github.com/octo/mystery") is None
        # The second call read the negative cache entry rather than re-probing.
        assert calls["n"] == 1

    def test_negative_entry_expires_after_the_short_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        _repo_visibility.slug_visibility("github.com/octo/mystery")
        # A read just past the short negative TTL treats the entry as expired.
        future = time.time() + _repo_visibility._UNKNOWN_TTL_S + 1
        monkeypatch.setattr(_repo_visibility.time, "time", lambda: future)
        assert _repo_visibility._read_visibility_cache("github.com/octo/mystery") is None

    def test_positive_verdict_uses_the_long_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")
        _repo_visibility.slug_visibility("github.com/octo/secret")
        # Just past the short negative TTL, a positive verdict is still fresh.
        future = time.time() + _repo_visibility._UNKNOWN_TTL_S + 1
        monkeypatch.setattr(_repo_visibility.time, "time", lambda: future)
        assert _repo_visibility._read_visibility_cache("github.com/octo/secret") == "PRIVATE"


class TestGitRemoteResolverTimeoutFailsSafe:
    """The git-remote origin resolver returns ``""`` on a timed-out ``git`` call."""

    def test_origin_url_via_git_timeout_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        assert _repo_visibility._origin_url_via_git(tmp_path) == ""


class TestClassifyAuthorSurvivesProbeTimeout:
    """The scanner-facing seam is fail-safe: a timed-out probe yields the untrusted (public) verdict."""

    def test_classify_author_does_not_raise_on_probe_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        result = classify_author("team/project", "someone", host_kind="gitlab")

        # Fail-safe direction: an unresolvable visibility is treated as PUBLIC, so an
        # unknown author is untrusted — the caller keeps dispatching rather than crashing.
        assert result.internal_repo is False
        assert result.untrusted is True
