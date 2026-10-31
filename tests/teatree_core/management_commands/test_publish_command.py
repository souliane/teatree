"""``t3 <overlay> publish``: the one sanctioned way to write a PR body, issue body or comment."""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import PullRequest, Ticket
from teatree.core.models.carrier_freeze import frozen_by_carrier
from tests._send_gate import allow_github_repos
from tests.factories import planned_ticket
from tests.teatree_core.publishing._recording_host import RecordingHost

_PR = "https://github.com/souliane/teatree/pull/5870"
_MEMBER = "https://github.com/souliane/teatree/pull/5871"
_OTHER_MEMBER = "https://github.com/souliane/teatree/pull/5872"
_ISSUE = "https://github.com/souliane/teatree/issues/12"


@pytest.fixture(autouse=True)
def _private_allowlisted_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: False)
    allow_github_repos("souliane/teatree")


def _publish(host: RecordingHost, *args: str) -> str:
    out = StringIO()
    with patch("teatree.core.management.commands.publish.build_publish_host", return_value=host):
        call_command("publish", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def _body(tmp_path: Path, text: str) -> str:
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestPublishCommand(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_a_clean_body_file_is_written_and_reported(self) -> None:
        tmp_path = self._tmp_path
        host = RecordingHost()
        out = _publish(host, "pr-body", _PR, "--body-file", _body(tmp_path, "Tidies it.\n\nRelates to #12"))
        assert len(host.writes) == 1
        assert "published" in out

    def test_a_closing_keyword_exits_one_with_zero_writes(self) -> None:
        tmp_path = self._tmp_path
        host = RecordingHost()
        with pytest.raises(SystemExit) as exited:
            _publish(host, "pr-body", _PR, "--body-file", _body(tmp_path, "Closes #12"))
        assert exited.value.code == 1
        assert host.writes == []

    def test_closes_lets_the_named_ref_through(self) -> None:
        tmp_path = self._tmp_path
        host = RecordingHost()
        args = ("issue-body", _ISSUE, "--body-file", _body(tmp_path, "Closes #12"), "--closes", "souliane/teatree#12")
        _publish(host, *args)
        assert len(host.writes) == 1

    def test_a_forge_that_appends_a_trailer_exits_one(self) -> None:
        tmp_path = self._tmp_path
        trailer = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>"  # privacy-scan:allow fake trailer fixture
        host = RecordingHost(appended=trailer)
        with pytest.raises(SystemExit) as exited:
            _publish(host, "comment", _ISSUE, "--body-file", _body(tmp_path, "A note."))
        assert exited.value.code == 1

    def test_a_missing_body_file_exits_one(self) -> None:
        tmp_path = self._tmp_path
        host = RecordingHost()
        with pytest.raises(SystemExit) as exited:
            _publish(host, "comment", _ISSUE, "--body-file", str(tmp_path / "absent.md"))
        assert exited.value.code == 1
        assert host.writes == []


class TestPublishFreezesTheDeclaredRound(TestCase):
    """A carrier body published with one ``Carries:`` line freezes the member tickets it names."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    @staticmethod
    def _tracked(url: str, issue: int) -> Ticket:
        ticket = planned_ticket(overlay="t3-teatree", issue_url=f"https://github.com/souliane/teatree/issues/{issue}")
        PullRequest.objects.create(
            ticket=ticket, overlay="t3-teatree", url=url, repo="souliane/teatree", iid=url.rsplit("/", 1)[1]
        )
        return ticket

    def _publish_carrier(self, text: str, *, target: str = _PR, kind: str = "pr-body") -> str:
        return _publish(RecordingHost(), kind, target, "--body-file", _body(self._tmp_path, text))

    def test_a_published_carries_line_freezes_the_member_and_marks_the_carrier(self) -> None:
        carrier, member = self._tracked(_PR, 1), self._tracked(_MEMBER, 2)
        self._publish_carrier(f"Merge round.\n\nCarries: {_MEMBER}")
        carrier.refresh_from_db()
        member.refresh_from_db()
        assert carrier.extra["carries"] == [_MEMBER]
        assert frozen_by_carrier(member) == _PR
        assert frozen_by_carrier(carrier) is None

    def test_republishing_without_a_member_releases_it(self) -> None:
        self._tracked(_PR, 1)
        kept, dropped = self._tracked(_MEMBER, 2), self._tracked(_OTHER_MEMBER, 3)
        self._publish_carrier(f"Carries: {_MEMBER} {_OTHER_MEMBER}")
        self._publish_carrier(f"Carries: {_MEMBER}")
        kept.refresh_from_db()
        dropped.refresh_from_db()
        assert frozen_by_carrier(kept) == _PR
        assert frozen_by_carrier(dropped) is None
        assert "carried_by" not in dropped.extra

    def test_republishing_with_no_line_releases_every_member_and_the_carrier_mark(self) -> None:
        carrier, member = self._tracked(_PR, 1), self._tracked(_MEMBER, 2)
        self._publish_carrier(f"Carries: {_MEMBER}")
        self._publish_carrier("Merge round dissolved.")
        carrier.refresh_from_db()
        member.refresh_from_db()
        assert "carries" not in carrier.extra
        assert frozen_by_carrier(member) is None

    def test_a_body_without_the_line_freezes_nothing(self) -> None:
        member = self._tracked(_MEMBER, 2)
        self._tracked(_PR, 1)
        self._publish_carrier("An ordinary body.")
        member.refresh_from_db()
        assert "carried_by" not in member.extra

    def test_a_comment_with_the_line_declares_no_round(self) -> None:
        self._tracked(_PR, 1)
        member = self._tracked(_MEMBER, 2)
        self._publish_carrier(f"Carries: {_MEMBER}", kind="comment")
        member.refresh_from_db()
        assert "carried_by" not in member.extra

    def test_a_refused_publish_freezes_nothing(self) -> None:
        self._tracked(_PR, 1)
        member = self._tracked(_MEMBER, 2)
        with pytest.raises(SystemExit):
            self._publish_carrier(f"Closes #12\nCarries: {_MEMBER}")
        member.refresh_from_db()
        assert "carried_by" not in member.extra

    def test_a_carrier_with_no_tracked_ticket_says_nothing_was_frozen(self) -> None:
        member = self._tracked(_MEMBER, 2)
        out = self._publish_carrier(f"Carries: {_MEMBER}")
        member.refresh_from_db()
        assert "carried_by" not in member.extra
        assert "nothing frozen" in out
