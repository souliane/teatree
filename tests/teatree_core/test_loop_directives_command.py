"""``manage.py loop_directives`` — the harness-neutral read surface (#4166 Phase 1).

Integration-first: drives the real command via ``call_command`` and asserts the
``{slot_id, cadence_seconds, text, scope, wakes_session}`` JSON contract any harness
consumes to build its own delivery adapter, the self-woken turn budget alongside it,
and the human view on stderr.
"""

import io
import json
from typing import Any

import django.test
from django.core.management import call_command

from teatree.core.models import Prompt
from teatree.loop.standing_directives import STANDING_DIRECTIVES, override_prompt_name


def _payload() -> dict[str, Any]:
    out = io.StringIO()
    call_command("loop_directives", "--json", stdout=out)
    parsed: dict[str, Any] = json.loads(out.getvalue())
    return parsed


def _directives() -> list[dict[str, Any]]:
    return _payload()["directives"]


class TestLoopDirectivesCommand(django.test.TestCase):
    def test_json_carries_the_five_key_contract_for_every_slot(self) -> None:
        directives = _directives()

        assert [d["slot_id"] for d in directives] == [s.slot_id for s in STANDING_DIRECTIVES]
        for entry in directives:
            assert set(entry) == {"slot_id", "cadence_seconds", "text", "scope", "wakes_session"}
            assert entry["scope"] in {"attended", "attended-singleton"}
            assert isinstance(entry["cadence_seconds"], int)
            assert isinstance(entry["wakes_session"], bool)
            assert entry["text"].strip()

    def test_json_carries_the_self_woken_turn_budget(self) -> None:
        assert _payload()["self_woken_turns_per_hour"] == {"per_session": 2, "per_host_singleton": 6}

    def test_json_reflects_a_prompt_row_override(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner text.")

        by_slot = {d["slot_id"]: d["text"] for d in _directives()}

        assert by_slot["standing-pr-board"] == "Owner text."

    def test_human_view_goes_to_stderr_leaving_stdout_a_clean_json_channel(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        call_command("loop_directives", stdout=out, stderr=err)

        assert out.getvalue().strip() == ""
        assert "standing-golden-rule" in err.getvalue()
