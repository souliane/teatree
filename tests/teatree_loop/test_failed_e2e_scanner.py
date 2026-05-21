"""Failed-E2E Slack-post scanner tests (#1295 capability E).

The scanner consumes :class:`FailedE2EWatcher` specs from the overlay
and emits one ``e2e.failure_detected`` signal per failing spec path
extracted from a recognised post.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.models import ScannedFailedE2E
from teatree.core.overlay import FailedE2EWatcher
from teatree.loop.scanners.failed_e2e_posts import FailedE2EPostsScanner
from teatree.types import RawAPIDict

CHANNEL = "C_E2E_FAIL"


@dataclass
class FakeMessaging:
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        del channel, ts
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        del channel, text, thread_ts
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        del channel, ts, text
        return {}

    def open_dm(self, user_id: str) -> str:
        del user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        del handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


class FailedE2EScannerTests(TestCase):
    def _watcher(self) -> FailedE2EWatcher:
        return FailedE2EWatcher(
            channel_id=CHANNEL,
            post_pattern=r"E2E failures?:",
            spec_pattern=r"\* (?P<spec>tests/[\w/.-]+\.spec\.ts)",
            agent_skill="t3:e2e",
        )

    def test_single_bullet_post_emits_one_signal(self) -> None:
        watcher = self._watcher()
        text = "E2E failures:\n* tests/foo/bar.spec.ts (timeout)"
        messages = {CHANNEL: [{"text": text, "ts": "1779.001"}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        signals = scanner.scan()
        assert len(signals) == 1
        assert signals[0].kind == "e2e.failure_detected"
        assert signals[0].payload["spec"] == "tests/foo/bar.spec.ts"

    def test_multi_bullet_post_emits_n_signals(self) -> None:
        watcher = self._watcher()
        text = (
            "E2E failures:\n"
            "* tests/a/one.spec.ts (timeout)\n"
            "* tests/b/two.spec.ts (assertion)\n"
            "* tests/c/three.spec.ts (network)\n"
        )
        messages = {CHANNEL: [{"text": text, "ts": "1779.002"}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        signals = scanner.scan()
        specs = sorted(s.payload["spec"] for s in signals)
        assert specs == ["tests/a/one.spec.ts", "tests/b/two.spec.ts", "tests/c/three.spec.ts"]

    def test_re_tick_is_idempotent(self) -> None:
        watcher = self._watcher()
        text = "E2E failures:\n* tests/foo.spec.ts (timeout)"
        messages = {CHANNEL: [{"text": text, "ts": "1779.003"}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        first = scanner.scan()
        second = scanner.scan()
        assert len(first) == 1
        # Second tick: same (channel, slack_ts, spec_path) → ledger
        # row already exists → zero new signals.
        assert second == []
        # One ledger row.
        assert ScannedFailedE2E.objects.count() == 1

    def test_message_without_post_pattern_match_is_ignored(self) -> None:
        watcher = self._watcher()
        messages = {CHANNEL: [{"text": "regular chat message", "ts": "1779.010"}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        assert scanner.scan() == []

    def test_message_with_missing_text_or_ts_is_ignored(self) -> None:
        watcher = self._watcher()
        messages = {
            CHANNEL: [
                {"ts": "1779.011"},  # no text
                {"text": "E2E failures:\n* tests/x.spec.ts"},  # no ts
                {"text": "", "ts": "1779.012"},  # blank text
                {"text": "E2E failures:\n* tests/y.spec.ts", "ts": ""},  # blank ts
                {"text": 123, "ts": "1779.013"},  # non-string text
            ],
        }

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        assert scanner.scan() == []

    def test_lines_without_spec_match_are_skipped(self) -> None:
        watcher = self._watcher()
        text = "E2E failures:\nSome context line with no spec ref\n* tests/real/spec.spec.ts (timeout)\nTrailing prose."
        messages = {CHANNEL: [{"text": text, "ts": "1779.020"}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        scanner = FailedE2EPostsScanner(
            backend=FakeMessaging(),
            watchers=[watcher],
            fetch_channel_history=fetch,
            overlay="test",
        )
        signals = scanner.scan()
        assert [s.payload["spec"] for s in signals] == ["tests/real/spec.spec.ts"]


class FailedE2EScannerFactoryTests(TestCase):
    """Cover :func:`failed_e2e_scanner_for` early-return branches."""

    def test_returns_none_when_overlay_missing(self) -> None:
        from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

        class _Backend:
            overlay = None
            messaging = FakeMessaging()
            name = "x"

        assert failed_e2e_scanner_for(_Backend()) is None

    def test_returns_none_when_messaging_missing(self) -> None:
        from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

        class _Backend:
            overlay = object()
            messaging = None
            name = "x"

        assert failed_e2e_scanner_for(_Backend()) is None

    def test_returns_none_when_overlay_has_no_watchers_getter(self) -> None:
        from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

        class _Config:
            pass  # no get_failed_e2e_watchers attr

        class _Overlay:
            config = _Config()

        class _Backend:
            overlay = _Overlay()
            messaging = FakeMessaging()
            name = "x"

        assert failed_e2e_scanner_for(_Backend()) is None

    def test_returns_none_when_watchers_empty(self) -> None:
        from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

        class _Config:
            def get_failed_e2e_watchers(self) -> list[FailedE2EWatcher]:
                return []

        class _Overlay:
            config = _Config()

        class _Backend:
            overlay = _Overlay()
            messaging = FakeMessaging()
            name = "x"

        assert failed_e2e_scanner_for(_Backend()) is None

    def test_returns_scanner_when_watchers_configured(self) -> None:
        from teatree.loop.scanners.failed_e2e_posts import (  # noqa: PLC0415
            FailedE2EPostsScanner,
            failed_e2e_scanner_for,
        )

        class _Config:
            def get_failed_e2e_watchers(self) -> list[FailedE2EWatcher]:
                return [
                    FailedE2EWatcher(
                        channel_id=CHANNEL,
                        post_pattern=r"failures?",
                        spec_pattern=r"(?P<spec>tests/\S+\.spec\.ts)",
                        agent_skill="t3:e2e",
                    ),
                ]

        class _Overlay:
            config = _Config()

        class _Backend:
            overlay = _Overlay()
            messaging = FakeMessaging()
            name = "ovl"

        scanner = failed_e2e_scanner_for(_Backend())
        assert isinstance(scanner, FailedE2EPostsScanner)
        assert scanner.overlay == "ovl"
