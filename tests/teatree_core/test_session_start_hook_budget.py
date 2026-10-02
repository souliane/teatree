"""An under-bounded SessionStart hook in the operator's own settings is named.

The repo's ``hooks.json`` is pinned by ``tests/test_hooks_json_declare_timeouts.py``.
This is the same pair of rules applied to the machine-local settings that test cannot
reach — the file an unbounded ``t3 doctor check`` came back through.
"""

import json
from pathlib import Path

import pytest

from teatree.core.session_start_hook_budget import SESSION_START_TIMEOUT_CEILING_S, advisory_text


def _settings(tmp_path: Path, hooks: list[dict]) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": hooks}]}}), encoding="utf-8")
    return path


class TestNamesTheUnderBoundedHook:
    def test_a_registration_with_no_timeout_is_named_with_its_command(self, tmp_path: Path) -> None:
        path = _settings(tmp_path, [{"type": "command", "command": "~/.claude/hooks/t3-doctor-session-start.sh"}])

        advisory = advisory_text(path)

        assert "t3-doctor-session-start.sh" in advisory
        assert "UNBOUNDED" in advisory

    def test_a_timeout_above_the_ceiling_is_named_with_its_value(self, tmp_path: Path) -> None:
        path = _settings(tmp_path, [{"command": "slow.sh", "timeout": SESSION_START_TIMEOUT_CEILING_S + 1}])

        advisory = advisory_text(path)

        assert "slow.sh" in advisory
        assert f"{SESSION_START_TIMEOUT_CEILING_S + 1}s exceeds" in advisory

    def test_every_offender_is_named_not_just_the_first(self, tmp_path: Path) -> None:
        path = _settings(tmp_path, [{"command": "a.sh"}, {"command": "b.sh", "timeout": 10}, {"command": "c.sh"}])

        advisory = advisory_text(path)

        assert "a.sh" in advisory
        assert "c.sh" in advisory

    def test_a_string_timeout_is_treated_as_unbounded_not_silently_clean(self, tmp_path: Path) -> None:
        # A JSON string like "45" is neither None nor int|float — the exact shape
        # that let a non-numeric timeout fall through both branches undetected.
        path = _settings(tmp_path, [{"command": "quoted.sh", "timeout": "45"}])

        advisory = advisory_text(path)

        assert "quoted.sh" in advisory
        assert "UNBOUNDED" in advisory


class TestStaysSilentOnAHealthyChain:
    def test_bounded_registrations_produce_nothing(self, tmp_path: Path) -> None:
        path = _settings(tmp_path, [{"command": "a.sh", "timeout": 10}, {"command": "b.sh", "timeout": 30}])

        assert advisory_text(path) == ""

    @pytest.mark.parametrize(
        "settings",
        [
            {},
            {"hooks": {}},
            {"hooks": {"SessionStart": []}},
            {"hooks": {"PreToolUse": [{"hooks": [{"command": "other.sh"}]}]}},
        ],
        ids=["empty", "no-hooks-key", "no-registrations", "another-event-only"],
    )
    def test_a_chain_with_nothing_on_it_produces_nothing(self, tmp_path: Path, settings: dict) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")

        assert advisory_text(path) == ""

    def test_an_absent_or_unparsable_file_produces_nothing(self, tmp_path: Path) -> None:
        malformed = tmp_path / "malformed.json"
        malformed.write_text("{not json", encoding="utf-8")

        assert advisory_text(tmp_path / "absent.json") == ""
        assert advisory_text(malformed) == ""


class TestOnlySessionStartIsJudged:
    def test_an_unbounded_hook_on_another_event_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [{"hooks": [{"command": "ok.sh", "timeout": 5}]}],
                        "Stop": [{"hooks": [{"command": "unbounded-stop.sh"}]}],
                    }
                }
            ),
            encoding="utf-8",
        )

        assert advisory_text(path) == ""
