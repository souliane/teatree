"""``handover create`` must persist what the session AUTHORED, and say so when it cannot (#3888).

Three independent links produced one failure: there was no way to author a payload
(the command took only a target), the only writable surface was the mirror file
that no reader reads back, and the live-DB fallback fired for every session that
had not yet compacted — reporting ``OK`` over a machine-derived inventory the
receiving session then claimed as if it were the hand-off.

The empty-payload refusal was already right in kind; it tested emptiness rather
than provenance, and a stub of untitled tickets and settled PRs is not empty. So:
an authoring path exists, the payload's SOURCE travels with it, and only a vetted
source (authored, or the session's own PreCompact snapshot) reports ``OK``.
"""

import json
import os
import pathlib
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from teatree.core.handover import HandoverPayload, PayloadSource, claim_handovers
from teatree.core.models import PullRequest, SessionHandover, Ticket
from tests._loop_principal_env import pinned_loop_principal

_AUTHORED = """# Hand-off

STANDING CONSTRAINT: never force-push.
TOP PRIORITY: the regression in the merge keystone.
DO NOT RESUME ANY OF THIS BY HAND.
"""


class _SessionCase(TestCase):
    """Pin this 'session' id and isolate the snapshot dir + XDG mirror."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = pathlib.Path(tmp_dir.name)
        self.enterContext(pinned_loop_principal("this-session"))
        dirs = mock.patch.dict(
            os.environ,
            {
                "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(self.tmp_path / "state"),
                "XDG_DATA_HOME": str(self.tmp_path / "xdg"),
            },
        )
        dirs.start()
        self.addCleanup(dirs.stop)
        os.environ.pop("T3_DATA_DIR", None)
        (self.tmp_path / "state").mkdir(parents=True, exist_ok=True)
        driver = mock.patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", return_value=[])
        driver.start()
        self.addCleanup(driver.stop)

    def _seed_snapshot(self, body: str = "PRECOMPACT STATE") -> None:
        (self.tmp_path / "state" / "t3-snapshot-this-session-precompact.md").write_text(body, encoding="utf-8")

    @staticmethod
    def _create(**kwargs) -> tuple[dict, str, int]:
        """Run ``handover create``; return ``(json, stderr, exit_code)`` (0 when it did not exit)."""
        out, err = StringIO(), StringIO()
        code = 0
        try:
            call_command("handover", "create", stdout=out, stderr=err, json_output=True, **kwargs)
        except SystemExit as exc:
            code = int(exc.code or 0)
        return json.loads(out.getvalue() or "{}"), err.getvalue(), code


class TestAuthoringPath(_SessionCase):
    def test_from_file_persists_the_file_bytes_verbatim(self) -> None:
        path = self.tmp_path / "handover.md"
        path.write_text(_AUTHORED, encoding="utf-8")
        data, _err, code = self._create(from_file=str(path), to="other-session", drive_subagents=False)
        assert code == 0
        assert data["payload_source"] == PayloadSource.AUTHORED.value
        assert SessionHandover.objects.get().payload == _AUTHORED, "the author's bytes, not a derived string"

    def test_body_persists_the_given_text(self) -> None:
        data, _err, code = self._create(body=_AUTHORED, to="other-session", drive_subagents=False)
        assert code == 0
        assert data["ok"] is True
        assert SessionHandover.objects.get().payload == _AUTHORED

    def test_authored_content_survives_create_then_claim(self) -> None:
        """The assertion that matters: what the receiver reads is what the author wrote."""
        self._seed_snapshot()  # present, and must lose to the authored body
        self._create(body=_AUTHORED, to="other-session", drive_subagents=False)
        payload, origin = claim_handovers("other-session")
        assert payload == _AUTHORED
        assert origin == "this-session"

    def test_from_file_and_body_together_are_refused(self) -> None:
        path = self.tmp_path / "handover.md"
        path.write_text(_AUTHORED, encoding="utf-8")
        data, err, code = self._create(from_file=str(path), body="other", to="other-session")
        assert code != 0
        assert data["ok"] is False
        assert "--body" in err
        assert "--from-file" in err
        assert SessionHandover.objects.count() == 0

    def test_an_unreadable_from_file_is_refused_not_silently_derived(self) -> None:
        data, err, code = self._create(from_file=str(self.tmp_path / "nope.md"), to="other-session")
        assert code != 0
        assert data["ok"] is False
        assert "nope.md" in err
        assert SessionHandover.objects.count() == 0, "a failed read must never fall back to a derived payload"


class TestUnvettedPayloadIsLoud(_SessionCase):
    def test_a_live_derived_payload_does_not_report_ok(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/o/r/issues/1", short_description="real work")
        data, err, code = self._create(to="other-session")
        assert data["payload_source"] == PayloadSource.LIVE.value
        assert data["ok"] is False, "reporting OK over a payload nobody vetted is the defect"
        assert code != 0
        assert "derived" in err.lower()
        assert "other-session" in err, "the warning must name who is about to receive it"
        assert SessionHandover.objects.count() == 1, "the row is still recorded — it is better than nothing"

    def test_a_snapshot_payload_reports_ok(self) -> None:
        self._seed_snapshot()
        data, _err, code = self._create(to="other-session")
        assert code == 0
        assert data["ok"] is True
        assert data["payload_source"] == PayloadSource.SNAPSHOT.value

    def test_an_empty_payload_is_still_refused(self) -> None:
        data, _err, code = self._create(to="other-session")
        assert code != 0
        assert data["ok"] is False
        assert data["empty_payload"] is True
        assert data["payload_source"] == PayloadSource.EMPTY.value

    def test_a_self_addressed_target_is_refused(self) -> None:
        """#3821 at the CLI seam: the row the receiver could never claim is never made."""
        data, err, code = self._create(body=_AUTHORED, to="this-session")
        assert code != 0
        assert data["ok"] is False
        assert "cannot hand off to itself" in err
        assert SessionHandover.objects.count() == 0


class TestLiveStateEmitsNoWorthlessLines(TestCase):
    def test_untitled_tickets_and_settled_prs_are_absent(self) -> None:
        Ticket.objects.create(issue_url="", short_description="")  # renders as "untitled"
        keeper = Ticket.objects.create(issue_url="https://github.com/o/r/issues/7", short_description="real work")
        merged = Ticket.objects.create(issue_url="https://github.com/o/r/issues/8")
        PullRequest.objects.create(
            ticket=merged, repo="o/r", iid="1", url="https://github.com/o/r/pull/1", state=PullRequest.State.CLOSED
        )
        PullRequest.objects.create(
            ticket=merged, repo="o/r", iid="2", url="https://github.com/o/r/pull/2", state=PullRequest.State.MERGED
        )
        PullRequest.objects.create(
            ticket=keeper, repo="o/r", iid="3", url="https://github.com/o/r/pull/3", state=PullRequest.State.OPEN
        )

        payload = HandoverPayload("s1").live_state()

        assert "untitled" not in payload, "a line carrying no information reads as inventory"
        assert "/pull/1" not in payload, "a CLOSED PR is not in flight"
        assert "/pull/2" not in payload, "a MERGED PR is not in flight"
        assert "/pull/3" in payload
        assert "real work" in payload
