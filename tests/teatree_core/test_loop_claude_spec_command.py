"""``manage.py loop_claude_spec <name>`` — emit one loop's native Claude ``/loop`` spec (#2650).

The CLI affordance the ``/t3:loops`` enable/disable skill reads: it prints the
stable ``slot_id`` + ``cron`` + ``prompt`` for a loop so the agent can ``CronCreate``
(enable) or ``CronList``→``CronDelete`` (disable) the exact native ``/loop``.
"""

import io
import json

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Loop, Prompt


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


def _run(*args: str, **kwargs: object) -> str:
    out = io.StringIO()
    call_command("loop_claude_spec", *args, stdout=out, **kwargs)
    return out.getvalue()


class TestLoopClaudeSpecCommand(django.test.TestCase):
    def test_json_emits_slot_id_cron_and_prompt(self) -> None:
        Loop.objects.create(name="spec-review", delay_seconds=300, prompt=_prompt(), enabled=True)
        payload = json.loads(_run("spec-review", json_output=True))
        assert payload == {
            "slot_id": "t3-loop-spec-review",
            "cron": "*/5 * * * *",
            "prompt": "Run `t3 loops tick --loop spec-review` in Bash, then briefly report the tick summary.",
        }

    def test_spec_is_emitted_even_for_a_disabled_row(self) -> None:
        # A disable flow runs ``t3 loop disable X`` (flips the row) THEN reads the
        # spec to ``CronDelete`` — so the spec must compute regardless of enabled.
        Loop.objects.create(name="spec-news", delay_seconds=86400, prompt=_prompt(), enabled=False)
        payload = json.loads(_run("spec-news", json_output=True))
        assert payload["slot_id"] == "t3-loop-spec-news"

    def test_human_output_names_each_field(self) -> None:
        Loop.objects.create(name="spec-ship", delay_seconds=300, prompt=_prompt(), enabled=True)
        out = _run("spec-ship")
        assert "t3-loop-spec-ship" in out
        assert "*/5 * * * *" in out
        assert "t3 loops tick --loop spec-ship" in out

    def test_unknown_loop_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            _run("nope")
