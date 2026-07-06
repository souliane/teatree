"""``persist_review_message`` — per-ticket review-message permalink record (#1098).

This JSON file is a *record* of where the sanctioned post landed (so the
permalink survives outside Slack), NOT a dedup oracle — dedup stays the
#1084 live-channel guard. The merge contract these tests pin: the file
accumulates one entry per MR URL and never clobbers a sibling MR's entry.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from teatree.core.review.review_message_cache import persist_review_message

_MR_385 = "https://gitlab.com/org/repo/-/merge_requests/385"
_MR_386 = "https://gitlab.com/org/repo/-/merge_requests/386"
_WHEN = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def test_writes_entry_under_ticket_iid(data_dir: Path) -> None:
    path = persist_review_message(
        mr_url=_MR_385,
        iid="385",
        permalink="https://team.slack.com/archives/C1/p1",
        channel="C1",
        when=_WHEN,
    )

    assert path == data_dir / "tickets" / "385" / "mr_review_messages.json"
    payload = json.loads(path.read_text())
    assert payload == {
        _MR_385: {
            "permalink": "https://team.slack.com/archives/C1/p1",
            "channel": "C1",
            "ts": "2026-05-19T12:00:00Z",
        }
    }


@pytest.mark.usefixtures("data_dir")
def test_iid_is_last_numeric_path_segment() -> None:
    path = persist_review_message(
        mr_url=_MR_386,
        iid="386",
        permalink="p",
        channel="C1",
        when=_WHEN,
    )
    assert path.parent.name == "386"


@pytest.mark.usefixtures("data_dir")
def test_merge_preserves_sibling_mr_entries() -> None:
    """A second MR sharing the ticket dir must NOT clobber the first MR's entry."""
    persist_review_message(
        mr_url=_MR_385,
        iid="385",
        permalink="link-385",
        channel="C1",
        when=_WHEN,
    )
    # Different MR but written into the same ticket dir (iid reused here on
    # purpose to exercise the read-merge-write path, not the iid derivation).
    path = persist_review_message(
        mr_url=_MR_386,
        iid="385",
        permalink="link-386",
        channel="C2",
        when=_WHEN,
    )

    payload = json.loads(path.read_text())
    assert set(payload) == {_MR_385, _MR_386}
    assert payload[_MR_385]["permalink"] == "link-385"
    assert payload[_MR_386]["permalink"] == "link-386"
    assert payload[_MR_386]["channel"] == "C2"


@pytest.mark.usefixtures("data_dir")
def test_reposting_same_mr_overwrites_only_its_own_entry() -> None:
    persist_review_message(
        mr_url=_MR_385,
        iid="385",
        permalink="old",
        channel="C1",
        when=_WHEN,
    )
    persist_review_message(
        mr_url=_MR_386,
        iid="385",
        permalink="sibling",
        channel="C1",
        when=_WHEN,
    )
    path = persist_review_message(
        mr_url=_MR_385,
        iid="385",
        permalink="new",
        channel="C1",
        when=_WHEN,
    )

    payload = json.loads(path.read_text())
    assert payload[_MR_385]["permalink"] == "new"
    assert payload[_MR_386]["permalink"] == "sibling"
