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

import errno
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from teatree import paths
from teatree.core.review.author_trust import classify_author
from teatree.hooks import _repo_visibility
from teatree.hooks._repo_visibility import PROBE_UNRUNNABLE, ForgeProbe, run_forge_tool
from teatree.utils.run import CommandFailedError, TimeoutExpired


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


class TestOnlyATransientForgeFailureIsAskedAgain:
    """A timeout, a 5xx, a stalled or dropped connection, or a refused process start is asked again; nothing else is."""

    @pytest.mark.parametrize(
        ("failure", "cause"),
        [
            (CommandFailedError(["glab"], 1, "", "glab: 401 Unauthorized\n"), "exit 1: glab: 401 Unauthorized"),
            (CommandFailedError(["glab"], 1, "", ""), "exit 1 with no stderr"),
            (
                CommandFailedError(["glab"], 1, "", "glab: 404 Not Found (project 512 Widgets)"),
                "exit 1: glab: 404 Not Found (project 512 Widgets)",
            ),
            (OSError(errno.ENOEXEC, "Exec format error"), PROBE_UNRUNNABLE),
        ],
    )
    def test_an_answer_is_not_asked_again_and_is_named(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception, cause: str
    ) -> None:
        run = Mock(side_effect=[failure, CompletedProcess(["glab"], 0, "[]", "")])
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", run)

        assert run_forge_tool("glab", ["api", "user"]) == ForgeProbe(stdout=None, unresolved=cause)
        assert run.call_count == 1

    @pytest.mark.parametrize(
        "transient",
        [
            TimeoutExpired("glab", 3),
            CommandFailedError(["gh"], 1, "", "HTTP 502: Bad Gateway (https://api.github.com/user)"),
            CommandFailedError(["glab"], 1, "", "glab: 503 Service Unavailable"),
            CommandFailedError(
                ["glab"], 1, "", "read tcp 10.0.0.2:51234->10.0.0.9:443: read: connection reset by peer"
            ),
            CommandFailedError(["glab"], 1, "", "Post https://gitlab.com/api/graphql: Connection reset by peer"),
            CommandFailedError(["glab"], 1, "", "dial tcp: lookup gitlab.com: Temporary failure in name resolution"),
            CommandFailedError(["glab"], 1, "", "fork/exec /usr/bin/ssh: Resource temporarily unavailable"),
            CommandFailedError(["gh"], 1, "", "Get https://api.github.com/user: dial tcp 10.0.0.9:443: i/o timeout"),
            CommandFailedError(["gh"], 1, "", "Get https://api.github.com/user: net/http: TLS handshake timeout"),
            CommandFailedError(["glab"], 1, "", "Get https://gitlab.com/api/v4/user: unexpected EOF"),
            CommandFailedError(
                ["glab"], 1, "", "context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
            ),
            BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
        ],
    )
    def test_a_transient_is_asked_again_with_the_same_command(
        self, monkeypatch: pytest.MonkeyPatch, transient: Exception
    ) -> None:
        run = Mock(side_effect=[transient, CompletedProcess(["glab"], 0, '{"username": "us"}', "")])
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", run)

        assert run_forge_tool("glab", ["api", "user"]) == ForgeProbe(stdout='{"username": "us"}')
        first, second = run.call_args_list
        assert (first.args, first.kwargs["env"]) == (second.args, second.kwargs["env"])

    def test_a_process_start_the_host_refused_is_named_apart_from_a_missing_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refused = BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", Mock(side_effect=[refused, refused]))

        assert run_forge_tool("glab", ["api", "user"]) == ForgeProbe(
            stdout=None,
            unresolved="a process start the host refused (EAGAIN), then a process start the host refused (EAGAIN)",
        )

    def test_each_attempt_takes_its_own_timeout_from_the_schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = Mock(side_effect=_raise_timeout)
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", run)

        assert run_forge_tool("glab", ["api", "user"]) == ForgeProbe(
            stdout=None, unresolved="timeout of 3s, then timeout of 5s"
        )
        assert [call.kwargs["timeout"] for call in run.call_args_list] == [3, 5]

    def test_no_attempt_starts_once_the_budget_is_spent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = Mock(side_effect=_raise_timeout)
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", run)
        affordable = {3: 2.5}

        assert run_forge_tool("glab", ["api", "user"], budget=affordable.get) == ForgeProbe(
            stdout=None, unresolved="timeout of 2.5s, then no time left in the hook budget"
        )
        assert [call.kwargs["timeout"] for call in run.call_args_list] == [2.5]


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


class TestNonPublicVerdictExpiresSooner:
    """Only private→public staleness can leak, so a NON-PUBLIC verdict expires far sooner.

    A cached ``PRIVATE`` makes every leak gate SKIP the scan for that slug. Once the
    operator flips the repo public, that skip is an unscanned public egress — so the
    verdict that authorises the skip must go stale in minutes, while a ``PUBLIC``
    verdict (whose staleness only ever over-scans) keeps the day-long cache.
    """

    def test_private_verdict_expires_before_the_public_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")
        _repo_visibility.slug_visibility("github.com/octo/secret")
        future = time.time() + _repo_visibility._NON_PUBLIC_TTL_S + 1
        monkeypatch.setattr(_repo_visibility.time, "time", lambda: future)
        assert _repo_visibility._read_visibility_cache("github.com/octo/secret") is None

    def test_public_verdict_is_still_fresh_at_that_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        _repo_visibility.slug_visibility("github.com/octo/open")
        future = time.time() + _repo_visibility._NON_PUBLIC_TTL_S + 1
        monkeypatch.setattr(_repo_visibility.time, "time", lambda: future)
        assert _repo_visibility._read_visibility_cache("github.com/octo/open") == "PUBLIC"

    def test_a_flipped_repo_is_reprobed_within_the_short_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The end-to-end consequence: the next publish after the flip reads PUBLIC, not the cached PRIVATE."""
        verdicts = iter(["PRIVATE", "PUBLIC"])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: next(verdicts))
        assert _repo_visibility.slug_visibility("github.com/octo/flipped") == "PRIVATE"
        future = time.time() + _repo_visibility._NON_PUBLIC_TTL_S + 1
        monkeypatch.setattr(_repo_visibility.time, "time", lambda: future)
        assert _repo_visibility.slug_visibility("github.com/octo/flipped") == "PUBLIC"


class TestGitRemoteResolverTimeoutFailsSafe:
    """The git-remote origin resolver returns ``""`` on a timed-out ``git`` call."""

    def test_origin_url_via_git_timeout_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        assert _repo_visibility._origin_url_via_git(tmp_path) == ""

    def test_the_origin_read_keeps_a_timeout_of_its_own(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = Mock(side_effect=_raise_timeout)
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", run)

        _repo_visibility._origin_url_via_git(tmp_path)

        assert run.call_args.kwargs["timeout"] == 5


class TestClassifyAuthorSurvivesProbeTimeout:
    """The scanner-facing seam is fail-safe: a timed-out probe yields the untrusted (public) verdict."""

    def test_classify_author_does_not_raise_on_probe_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_repo_visibility, "run_allowed_to_fail", _raise_timeout)

        result = classify_author("team/project", "someone", pr_url="https://gitlab.com/team/project/pull/1")

        # Fail-safe direction: an unresolvable visibility is treated as PUBLIC, so an
        # unknown author is untrusted — the caller keeps dispatching rather than crashing.
        assert result.internal_repo is False
        assert result.untrusted is True


class TestVisibilityCacheIsHostWideNotPerWorktree:
    """A remote's visibility is a fact about the FORGE, so its cache is host-wide.

    ``paths.DATA_DIR`` is auto-isolated per worktree, and the visibility cache
    used to follow it. Isolation buys nothing here — the cached fact belongs to
    the remote, not to the checkout asking — and it cost determinism: each
    worktree froze its own answer, so one URL resolved PRIVATE in one worktree
    and UNKNOWN in the next.
    """

    def test_cache_root_ignores_a_worktree_isolated_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        before = _repo_visibility._cache_root()
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "teatree-worktrees" / "0ff8d4f5c527")

        assert _repo_visibility._cache_root() == before

    def test_explicit_data_dir_override_still_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anti-vacuity: the cache root is not hardcoded — a test/sandbox override still redirects it."""
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "sandbox"))

        assert _repo_visibility._cache_root() == tmp_path / "sandbox"
