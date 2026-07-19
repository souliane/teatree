"""Per-watcher fault isolation for ``failed_e2e_posts`` (F5.7).

A bad ``post_pattern`` / ``spec_pattern`` (raising ``re.error`` when compiled)
or a channel fetch failure on one watcher must not kill the watchers queued
after it. The DB-not-migrated OperationalError/ProgrammingError stays global.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.overlay import FailedE2EWatcher
from teatree.loop.scanners.failed_e2e_posts import FailedE2EPostsScanner
from teatree.types import RawAPIDict

_GOOD_CHANNEL = "C_GOOD"
_BAD_REGEX_CHANNEL = "C_BADREGEX"
_RAISING_CHANNEL = "C_RAISES"


@dataclass
class _Messaging:
    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


@dataclass
class _Fetch:
    by_channel: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    raises_for: frozenset[str] = frozenset()
    seen: list[str] = field(default_factory=list)

    def __call__(self, *, channel: str) -> list[RawAPIDict]:
        self.seen.append(channel)
        if channel in self.raises_for:
            msg = f"history fetch failed for {channel}"
            raise RuntimeError(msg)
        return list(self.by_channel.get(channel, ()))


def _good_watcher() -> FailedE2EWatcher:
    return FailedE2EWatcher(
        channel_id=_GOOD_CHANNEL,
        post_pattern=r"E2E failures?:",
        spec_pattern=r"\* (?P<spec>tests/[\w/.-]+\.spec\.ts)",
        agent_skill="t3:e2e",
    )


def _bad_regex_watcher() -> FailedE2EWatcher:
    # An unbalanced group is a genuine ``re.error`` at compile time.
    return FailedE2EWatcher(
        channel_id=_BAD_REGEX_CHANNEL,
        post_pattern=r"E2E failures?:",
        spec_pattern=r"(?P<spec>tests/[",
        agent_skill="t3:e2e",
    )


def _raising_watcher() -> FailedE2EWatcher:
    return FailedE2EWatcher(
        channel_id=_RAISING_CHANNEL,
        post_pattern=r"E2E failures?:",
        spec_pattern=r"\* (?P<spec>tests/[\w/.-]+\.spec\.ts)",
        agent_skill="t3:e2e",
    )


_GOOD_MSG: RawAPIDict = {"text": "E2E failures:\n* tests/foo/bar.spec.ts (timeout)", "ts": "1779.001"}


class PerWatcherIsolationTests(TestCase):
    def test_bad_regex_watcher_does_not_kill_later_watchers(self) -> None:
        fetch = _Fetch(by_channel={_GOOD_CHANNEL: [_GOOD_MSG]})
        scanner = FailedE2EPostsScanner(
            backend=_Messaging(),
            watchers=[_bad_regex_watcher(), _good_watcher()],
            fetch_channel_history=fetch,
            overlay="test",
        )

        signals = scanner.scan()

        # The good watcher still produced its signal despite the bad-regex watcher.
        assert [s.payload["spec"] for s in signals] == ["tests/foo/bar.spec.ts"]

    def test_fetch_failure_on_one_watcher_isolated(self) -> None:
        fetch = _Fetch(
            by_channel={_GOOD_CHANNEL: [_GOOD_MSG]},
            raises_for=frozenset({_RAISING_CHANNEL}),
        )
        scanner = FailedE2EPostsScanner(
            backend=_Messaging(),
            watchers=[_raising_watcher(), _good_watcher()],
            fetch_channel_history=fetch,
            overlay="test",
        )

        signals = scanner.scan()

        assert fetch.seen == [_RAISING_CHANNEL, _GOOD_CHANNEL]
        assert [s.payload["spec"] for s in signals] == ["tests/foo/bar.spec.ts"]

    def test_all_healthy_watchers_scan(self) -> None:
        other = "C_GOOD2"
        fetch = _Fetch(
            by_channel={
                _GOOD_CHANNEL: [_GOOD_MSG],
                other: [{"text": "E2E failures:\n* tests/baz/qux.spec.ts (net)", "ts": "1779.002"}],
            },
        )
        second = FailedE2EWatcher(
            channel_id=other,
            post_pattern=r"E2E failures?:",
            spec_pattern=r"\* (?P<spec>tests/[\w/.-]+\.spec\.ts)",
            agent_skill="t3:e2e",
        )
        scanner = FailedE2EPostsScanner(
            backend=_Messaging(),
            watchers=[_good_watcher(), second],
            fetch_channel_history=fetch,
            overlay="test",
        )

        signals = scanner.scan()

        assert sorted(s.payload["spec"] for s in signals) == ["tests/baz/qux.spec.ts", "tests/foo/bar.spec.ts"]
