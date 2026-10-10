"""No surface the owner reads says "deferred" or "Pending question" (#4990).

The owner never sees the queue's internal name: not in a card, a bump, the digest, a click update, the
``questions`` command, its help, nor the dispatch loop's line on the loops page.
"""

import contextlib
import datetime as dt
import io
import json
from collections.abc import Iterator

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core._checking_gather import deferred_questions
from teatree.core.models import DeferredQuestion
from teatree.core.owner_question_message import (
    answered_line,
    bump_text,
    closed_message,
    digest_text,
    render_blocks,
    render_text,
    withheld_message,
)
from teatree.loops.seed import seed_default_loops_and_prompts
from tests._owner_channel import OWNER_DECISION

_BANNED = ("deferred", "pending question", "typed reply")


def _command_output(*args: str) -> str:
    out = io.StringIO()
    with contextlib.suppress(SystemExit):
        call_command("questions", *args, stdout=out, stderr=out)
    return out.getvalue()


def _every_owner_message(row: DeferredQuestion) -> Iterator[tuple[str, str]]:
    now = dt.datetime.now(dt.UTC)
    yield "card text", render_text(row)
    yield "card blocks", json.dumps(render_blocks(row))
    yield "bump", bump_text(row, now=now + dt.timedelta(hours=2))
    yield "digest", digest_text([row], now=now)
    yield "answered line", answered_line(row)
    yield "closed card", closed_message(row, "This question no longer needs an answer.").text
    yield "withheld card", withheld_message().text


class TestNoSlackMessageSaysDeferred(TestCase):
    def test_every_message_the_owner_can_receive_is_free_of_the_queue_name(self) -> None:
        row = DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)

        for surface, text in _every_owner_message(row):
            for banned in _BANNED:
                with self.subTest(surface=surface, banned=banned):
                    assert banned not in text.lower()


class TestNoCommandTextSaysDeferred(TestCase):
    def test_the_empty_list_reads_no_open_questions(self) -> None:
        assert _command_output("list").strip() == "no open questions."

    def test_the_list_title_counts_open_questions(self) -> None:
        DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)

        output = _command_output("list")

        assert "1 open question(s)" in output
        assert "deferred" not in output.lower()

    def test_the_command_help_never_says_deferred(self) -> None:
        for args in (("--help",), ("list", "--help"), ("record", "--help")):
            with self.subTest(args=args):
                assert "deferred" not in _command_output(*args).lower()

    def test_the_group_listing_never_says_deferred(self) -> None:
        group = DJANGO_GROUPS["questions"]

        text = " ".join([group.help_text, *(help_ for _name, help_ in group.subcommands)])

        assert "deferred" not in text.lower()


class TestNoPageSaysDeferred(TestCase):
    def test_the_loops_page_describes_the_dispatch_loop_with_owner_questions(self) -> None:
        seed_default_loops_and_prompts()

        body = self.client.get(reverse("dash:loops_table")).content.decode()

        assert "posts owner questions." in body
        assert "deferred" not in body.lower()

    def test_the_checking_needs_you_group_names_no_deferred_question(self) -> None:
        DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)

        items = deferred_questions(overlay_slug="acme")

        assert items
        for item in items:
            assert "deferred" not in f"{item.label} {item.detail}".lower()
