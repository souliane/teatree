"""Tests for ``t3 <overlay> pending_chat`` management command (#1063)."""

import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.management.commands.pending_chat import Command
from teatree.core.models import DmContext, PendingChatInjection
from teatree.types import RawAPIDict
from tests._factory_slack import OWNER_DM, OWNER_ID, SLACK_OVERLAY, FakeOwnerDm, live_factory_slack

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


def _call_stderr(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stderr=buf)
    return buf.getvalue()


class TestListSubcommand:
    def test_list_empty_says_no_rows(self) -> None:
        out = _call("pending_chat", "list")
        assert "no inbound rows" in out

    def test_list_default_window_only_recent(self) -> None:
        old = PendingChatInjection.record(channel="D", slack_ts="1", text="ancient")
        recent = PendingChatInjection.record(channel="D", slack_ts="2", text="fresh")
        assert old is not None
        assert recent is not None
        old.received_at = timezone.now() - timedelta(hours=5)
        old.save(update_fields=["received_at"])

        out = _call("pending_chat", "list")

        assert "fresh" in out
        assert "ancient" not in out

    def test_list_all_includes_old(self) -> None:
        old = PendingChatInjection.record(channel="D", slack_ts="1", text="ancient")
        assert old is not None
        old.received_at = timezone.now() - timedelta(hours=5)
        old.save(update_fields=["received_at"])

        out = _call("pending_chat", "list", "--all")

        assert "ancient" in out

    def test_list_marks_question_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1", text="why is this red?")

        out = _call("pending_chat", "list")

        assert "question" in out

    def test_list_marks_answered_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1", text="why?")
        PendingChatInjection.agent_answered_question("1")

        out = _call("pending_chat", "list")

        assert "answered" in out

    def test_list_marks_a_row_the_answer_loop_already_handled(self) -> None:
        """A handled row must not print identically to a queued one.

        The measured failure: row #52 ("where do we stand?") had been
        :eyes:-reacted, stamped ``loop_replied_at``/``delegated`` and had an
        ``answering`` Task dispatched — yet printed as ``#52 [question]``,
        the same shape as an untouched row. Read as "still queued", which is
        what produced the false "the loop selects nothing" diagnosis.
        """
        row = PendingChatInjection.record(channel="D", slack_ts="1", text="where do we stand?")
        assert row is not None
        assert row.mark_loop_replied(PendingChatInjection.AnswerKind.DELEGATED) is True

        out = _call("pending_chat", "list")

        assert "delegated" in out

    def test_list_marks_an_untouched_row_as_queued(self) -> None:
        """The inverse control: a genuinely unhandled row says so, and says nothing else."""
        PendingChatInjection.record(channel="D", slack_ts="1", text="where do we stand?")

        out = _call("pending_chat", "list")

        assert "queued" in out
        assert "delegated" not in out
        assert "consumed" not in out


class TestMarkAnsweredSubcommand:
    def test_stamps_matching_row(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_unknown_ts_says_zero_stamped(self) -> None:
        out = _call("pending_chat", "mark-answered", "ts-not-here")

        assert "stamped 0 row" in out

    def test_second_call_is_zero_stamped(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?")
        _call("pending_chat", "mark-answered", "ts-x")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 0 row" in out

    def test_stamps_every_row_sharing_the_ts(self) -> None:
        """The stamp keys on ``slack_ts`` alone — it does not narrow by overlay.

        Two rows sharing a ``ts`` are the same user DM recorded under two
        overlays — answering it stamps both. The old exact-overlay filter
        left the other row unanswered so the gate nagged forever.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovA")
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovB")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 2 row" in out
        assert PendingChatInjection.objects.get(overlay="ovA").answered_at is not None
        assert PendingChatInjection.objects.get(overlay="ovB").answered_at is not None

    def test_cross_overlay_answer_clears_gate(self) -> None:
        """Sub-case (b): answering a row recorded under a different overlay clears it.

        The recording overlay (``overlay-alpha``) and the answering
        session's overlay differ — the common case in a concurrent
        multi-overlay deployment. The old exact-overlay filter stamped 0
        rows; the ts-keyed stamp clears the row regardless.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-cross", text="why?", overlay="overlay-alpha")

        out = _call("pending_chat", "mark-answered", "ts-cross")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_empty_ts_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _call("pending_chat", "mark-answered", "   ")

        assert excinfo.value.code == 2

    def test_bare_invocation_clears_row_recorded_under_other_overlay(self) -> None:
        """The bare ``mark-answered <ts>`` clears any matching ``ts``.

        This is what the agent runs after a direct reply. It must clear the
        row regardless of which overlay recorded it — ``slack_ts`` is the
        unique idempotency key and the stamp keys on it alone, symmetric
        with the unscoped gate.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-bare", text="why?", overlay="overlay-alpha")

        out = _call("pending_chat", "mark-answered", "ts-bare")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_overlay_env_var_cannot_strand_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set ``T3_OVERLAY_NAME`` differing from the recording overlay still clears.

        Regression guard for the original defect: the CLI env-fallback used
        to forward a non-empty overlay into a scoped filter, stamping 0 rows
        whenever the answering session's overlay differed from the recording
        overlay. The stamp ignores overlay entirely now, so a differing
        ``T3_OVERLAY_NAME`` cannot strand the row.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-env", text="why?", overlay="overlay-alpha")
        monkeypatch.setenv("T3_OVERLAY_NAME", "overlay-beta")

        out = _call("pending_chat", "mark-answered", "ts-env")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None


PROMPT = "  deploy it?\nsecond line  \n"
_WAKE = "teatree.core.management.commands.pending_chat.wake_slack_answer"


def _relay_argv(*args: str) -> int | None:
    """Run ``pending_chat relay`` through the argv path; the exit code, ``None`` for a clean return."""
    try:
        Command().run_from_argv(["manage.py", "pending_chat", "relay", *args])
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 1
    return None


class TestRelaySubcommand:
    def test_a_live_relay_prints_the_receipt_exits_zero_and_wakes_the_cycle_after_commit(
        self, capsys: pytest.CaptureFixture[str], django_capture_on_commit_callbacks
    ) -> None:
        dm = FakeOwnerDm()
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}),
            live_factory_slack(dm),
            patch(_WAKE) as wake,
            django_capture_on_commit_callbacks(execute=True),
        ):
            code = _relay_argv("--relay-id", "r1", "--text", PROMPT)

        row = PendingChatInjection.objects.get()
        [post] = dm.posts
        assert code is None
        assert capsys.readouterr().out.strip() == (
            f"t3-relay-receipt: pending_chat={row.pk} ts={post['ts']} "
            f"permalink=https://slack.test/archives/{dm.channel}/p{post['ts'].replace('.', '')}"
        )
        assert (row.relayed_by_session, row.text, row.overlay) == ("sess-env", PROMPT, SLACK_OVERLAY)
        assert (row.slack_ts, row.channel, row.user_id) == (post["ts"], dm.channel, OWNER_ID)
        wake.enqueue.assert_called_once_with()

    def test_the_prompt_can_come_from_a_file_verbatim(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text(PROMPT, encoding="utf-8")
        with live_factory_slack(FakeOwnerDm()), patch(_WAKE):
            code = _relay_argv("--relay-id", "r1", "--text-file", str(prompt_file))

        assert code is None
        assert PendingChatInjection.objects.get().text == PROMPT

    def test_a_retry_with_the_same_relay_id_prints_the_same_receipt_and_wakes_once(
        self, capsys: pytest.CaptureFixture[str], django_capture_on_commit_callbacks
    ) -> None:
        with live_factory_slack(FakeOwnerDm()), patch(_WAKE) as wake, django_capture_on_commit_callbacks(execute=True):
            _relay_argv("--relay-id", "r1", "--text", PROMPT)
            first = capsys.readouterr().out
            _relay_argv("--relay-id", "r1", "--text", PROMPT)
            again = capsys.readouterr().out

        assert first == again
        assert PendingChatInjection.objects.count() == 1
        wake.enqueue.assert_called_once_with()

    def test_a_relay_refused_while_slack_is_not_live_exits_three_posting_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(_WAKE) as wake:
            code = _relay_argv("--relay-id", "r1", "--text", PROMPT)

        assert code == 3
        assert capsys.readouterr().out.startswith("t3-relay-refused: slack_not_live")
        assert PendingChatInjection.objects.count() == 0
        wake.enqueue.assert_not_called()

    def test_a_pasted_credential_is_refused_with_exit_three(self, capsys: pytest.CaptureFixture[str]) -> None:
        dm = FakeOwnerDm()
        with live_factory_slack(dm), patch(_WAKE):
            code = _relay_argv("--relay-id", "r1", "--text", "token sk-" + "ant-api03-" + "a" * 40)

        assert code == 3
        assert capsys.readouterr().out.startswith("t3-relay-refused: secret")
        assert dm.posts == []

    def test_a_post_that_does_not_land_exits_four_with_no_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        with live_factory_slack(FakeOwnerDm(fail_post=True)), patch(_WAKE) as wake:
            code = _relay_argv("--relay-id", "r1", "--text", PROMPT)

        assert code == 4
        assert capsys.readouterr().out.startswith("t3-relay-refused: delivery_failed")
        assert PendingChatInjection.objects.count() == 0
        wake.enqueue.assert_not_called()

    @pytest.mark.parametrize(
        ("prompt", "dm_options", "exit_code", "marker"),
        [
            pytest.param("  \n ", {}, 3, "t3-relay-refused: blank", id="blank"),
            pytest.param("x" * 38_001, {}, 3, "t3-relay-refused: over 38000", id="over-the-slack-limit"),
            pytest.param(PROMPT, {"read_back": False}, 4, "t3-relay-refused: delivery_failed", id="not-read-back"),
        ],
    )
    def test_an_unrelayable_or_unconfirmed_prompt_exits_with_its_code_leaving_no_row_and_no_wake(
        self,
        capsys: pytest.CaptureFixture[str],
        prompt: str,
        dm_options: dict[str, bool],
        exit_code: int,
        marker: str,
    ) -> None:
        with live_factory_slack(FakeOwnerDm(**dm_options)), patch(_WAKE) as wake:
            code = _relay_argv("--relay-id", "r1", "--text", prompt)

        assert code == exit_code
        assert capsys.readouterr().out.startswith(marker)
        assert PendingChatInjection.objects.count() == 0
        wake.enqueue.assert_not_called()

    def test_a_missing_relay_id_or_prompt_is_a_usage_error(self) -> None:
        assert _relay_argv("--text", PROMPT) == 2
        assert _relay_argv("--relay-id", "r1") == 2


class TestRelaysSubcommand:
    def _relay_row(self, ts: str, session: str | None, *, age_minutes: float = 10) -> PendingChatInjection:
        row = PendingChatInjection.record(
            channel="D1",
            slack_ts=ts,
            text=f"prompt {ts}",
            overlay="t3-slack",
            context=DmContext(user_id="UOWNER", relayed_by_session=session),
        )
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(
            received_at=timezone.now() - timedelta(minutes=age_minutes)
        )
        return row

    def test_a_session_sees_its_own_open_relays_and_no_session_sees_all_of_them(self) -> None:
        mine = self._relay_row("1.0", "s1")
        theirs = self._relay_row("2.0", "s2")
        self._relay_row("3.0", "s1", age_minutes=1)

        own = json.loads(_call("pending_chat", "relays", "--session", "s1", "--json"))
        everyone = json.loads(_call("pending_chat", "relays", "--json"))

        assert [row["id"] for row in own] == [mine.pk]
        assert {row["id"]: row["session"] for row in everyone} == {mine.pk: "s1", theirs.pk: "s2"}
        assert own[0] == {
            "id": mine.pk,
            "session": "s1",
            "channel": "D1",
            "slack_ts": "1.0",
            "state": "unanswered",
            "text": "prompt 1.0",
        }

    def test_the_human_view_names_each_open_relay_and_says_when_there_are_none(self) -> None:
        assert "no open relays" in _call_stderr("pending_chat", "relays")

        row = self._relay_row("1.0", "s1")

        assert f"#{row.pk}" in _call_stderr("pending_chat", "relays", "--session", "s1")


class _SlackWebApi:
    """The Slack Web API behind the bot's HTTP client, for one owner DM: only what a relay calls, each call recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.messages: dict[str, RawAPIDict] = {}

    def post(self, method: str, *, token: str, json: RawAPIDict, idempotent: bool = True) -> RawAPIDict:
        _ = (token, idempotent)
        self.calls.append(method)
        assert method == "chat.postMessage", method
        ts = f"1700000000.{len(self.messages) + 1:06d}"
        self.messages[ts] = {"ts": ts, "text": json["text"], "channel": json["channel"]}
        return {"ok": True, "ts": ts, "channel": json["channel"]}

    def get(self, method: str, *, token: str, params: RawAPIDict) -> RawAPIDict:
        _ = token
        self.calls.append(method)
        if method == "conversations.history":
            message = self.messages.get(str(params["latest"]))
            return {"ok": True, "messages": [message] if message else []}
        assert method == "chat.getPermalink", method
        return {"ok": True, "permalink": f"https://slack.test/archives/{params['channel']}/p{params['message_ts']}"}


LONG_PROMPT = (
    "Should I deploy the hotfix to production right now, or wait for the nightly window so that the on-call "
    "engineer can watch the rollout and roll it back if the error rate moves?"
)


class TestRelayThroughTheRealSlackBot:
    @pytest.mark.parametrize("owner_dm_only", [False, True], ids=["full-scope", "dm-only"])
    def test_a_long_prompt_lands_in_the_owner_dm_verbatim_with_only_http_stubbed(
        self, capsys: pytest.CaptureFixture[str], *, owner_dm_only: bool
    ) -> None:
        api = _SlackWebApi()
        bot = SlackBotBackend(
            bot_token="xoxb-bot", user_id=OWNER_ID, dm_channel_id=OWNER_DM, owner_dm_only=owner_dm_only
        )
        with (
            patch.object(bot._http, "post", side_effect=api.post),
            patch.object(bot._http, "get", side_effect=api.get),
            live_factory_slack(bot),
            patch(_WAKE),
        ):
            code = _relay_argv("--relay-id", "r1", "--text", LONG_PROMPT)

        row = PendingChatInjection.objects.get()
        assert code is None
        assert capsys.readouterr().out.startswith(f"t3-relay-receipt: pending_chat={row.pk} ts={row.slack_ts} ")
        assert api.calls.count("chat.postMessage") == 1
        assert (row.channel, row.user_id, row.text) == (OWNER_DM, OWNER_ID, LONG_PROMPT)
        assert row.slack_ts in api.messages
