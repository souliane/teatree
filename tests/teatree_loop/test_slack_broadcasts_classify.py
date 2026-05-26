"""Manual ``all_merged`` flips survive a rescan (#1320).

The bug: ``slack_broadcasts._classify()`` only emits ``ALL_MERGED`` when every
referenced MR is both merged AND approved. Any other skip signal — my_notes,
non-self reactions, author=me, upvotes — does NOT survive a rescan because
``ScannedBroadcast.record`` overwrites the row's classification when it
disagrees with the freshly-derived one. Operators (or sub-agents) that flip a
row to ``all_merged`` because the broadcast is socially "done" see the next
``t3 loop tick`` revert it back to ``pending``.

Fix per Option A: a sticky ``manually_classified`` flag on the row. When set,
``record`` no-ops on the row's classification and keeps the operator's verdict
intact. The flag is set explicitly via
:meth:`ScannedBroadcast.mark_manually_classified`; clearing requires an explicit
reset (not modelled here — only the sticky-survives-rescan invariant).
"""

from django.test import TestCase

from teatree.core.models import BroadcastObservation, ScannedBroadcast
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

CHANNEL = "C0AM3TENTLK"
TS = "1779201478.501469"
MR_URL = "https://gitlab.example.com/team/project/-/merge_requests/7432"


class FakeMessaging:
    def __init__(self) -> None:
        self.react_calls: list[tuple[str, str, str]] = []

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


def _fetcher(messages: list[RawAPIDict]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        _ = channel
        return list(messages)

    return fetch


def _classifier(state: MrState):
    def classify(urls):
        return [state for _ in urls]

    return classify


def _message(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "user": "USRG", "type": "message"}


class TestManuallyClassifiedSurvivesRescan(TestCase):
    def test_manually_classified_survives_rescan(self) -> None:
        # Seed: a previous tick recorded the broadcast as ``pending`` (the MR was open).
        seed = ScannedBroadcast.record(
            BroadcastObservation(
                channel=CHANNEL,
                slack_ts=TS,
                mr_urls=[MR_URL],
                classification=ScannedBroadcast.Classification.PENDING.value,
            ),
        )
        assert seed is not None

        # Operator (or sub-agent) flips the row to ``all_merged`` because the
        # broadcast is socially "done" (my_notes / reaction / author=me) even
        # though the MR is still open. Calls the new sticky API.
        seed.mark_manually_classified(ScannedBroadcast.Classification.ALL_MERGED)

        # Re-scan: the MR is still open, so the auto-derived classification
        # is still ``pending``. Without the sticky flag, ``record`` would
        # overwrite the manual flip back to ``pending``.
        backend = FakeMessaging()
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher([_message(f"please review {MR_URL}", TS)]),
            classify_mrs=_classifier(MrState(url=MR_URL, merged=False, approved=False)),
        )
        scanner.scan()

        row = ScannedBroadcast.objects.get(pk=seed.pk)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.manually_classified is True

    def test_mark_manually_classified_is_idempotent(self) -> None:
        row = ScannedBroadcast.record(
            BroadcastObservation(
                channel=CHANNEL,
                slack_ts=TS,
                mr_urls=[MR_URL],
                classification=ScannedBroadcast.Classification.PENDING.value,
            ),
        )
        assert row is not None

        first = row.mark_manually_classified(ScannedBroadcast.Classification.ALL_MERGED)
        second = row.mark_manually_classified(ScannedBroadcast.Classification.ALL_MERGED)

        assert first is True
        assert second is False
        row.refresh_from_db()
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.manually_classified is True

    def test_auto_derived_classification_change_still_applies_when_not_manual(self) -> None:
        # Pre-existing behaviour must keep working: when ``manually_classified``
        # is False, a re-classification (pending → all_merged) still flows
        # through.
        ScannedBroadcast.record(
            BroadcastObservation(
                channel=CHANNEL,
                slack_ts=TS,
                mr_urls=[MR_URL],
                classification=ScannedBroadcast.Classification.PENDING.value,
            ),
        )
        backend = FakeMessaging()
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher([_message(f"please review {MR_URL}", TS)]),
            classify_mrs=_classifier(MrState(url=MR_URL, merged=True, approved=True)),
        )
        scanner.scan()

        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.manually_classified is False
