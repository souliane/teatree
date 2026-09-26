"""``t3 settings compare`` — the Compare Instances page answered in a terminal.

What is pinned here is the honesty the page carries with layout and a terminal loses by
default. A box that could not answer must be NAMED with its reason, because a configured
instance quietly dropped reads as agreement between the ones that did answer. The
comparability verdict must reach the reader BEFORE the rows it disqualifies, because a value
diff printed as though it were configuration drift, when the boxes do not even declare the
same settings, is less truthful than printing nothing. And the comparison itself must come
from the page's own seam, so the two surfaces cannot answer differently.
"""

import json
from typing import Any
from unittest.mock import patch

import django.core.management
import pytest
from typer.testing import CliRunner

import teatree.cli.settings as settings_cli
from teatree.cli import app
from teatree.core.settings.settings_peers import PeerSnapshot
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT

runner = CliRunner()

_DECLARED = ("merge_wip", "autonomy")


class TestSettingsCompareCliDelegation:
    def test_the_typer_command_only_bootstraps_and_delegates(self) -> None:
        with (
            patch.object(settings_cli, "ensure_django") as ensure_django,
            patch.object(django.core.management, "call_command") as management_command,
        ):
            result = runner.invoke(app, ["settings", "compare"])

        assert result.exit_code == 0
        ensure_django.assert_called_once_with()
        management_command.assert_called_once_with(
            "settings_compare",
            snapshots=[],
            kinds=[],
            timeout=None,
            full=False,
            json_output=False,
        )


def _payload(stored: dict[str, dict[str, Any]], declares: tuple[str, ...], **fingerprint: Any) -> dict[str, Any]:
    return {
        "format": SNAPSHOT_FORMAT,
        "captured_at": "2026-01-01T00:00:00Z",
        "fingerprint": fingerprint,
        "registry": {"settings": {key: {"syncable": True, "sync_note": "", "category": ""} for key in declares}},
        "values": {"settings": stored, "defaults": {}, "provenance": {}, "seed": {}, "seed_shipped": {}},
    }


def _box(
    label: str, stored: dict[str, dict[str, Any]], declares: tuple[str, ...] = _DECLARED, **fingerprint: Any
) -> PeerSnapshot:
    return PeerSnapshot(
        label=label, url=f"http://127.0.0.1/{label}", note="", payload=_payload(stored, declares, **fingerprint)
    )


def _no_service(label: str) -> PeerSnapshot:
    """A box with no teatree serving on it: the forward carried the request, the far end 404'd."""
    return PeerSnapshot(
        label=label,
        url=f"http://127.0.0.1/{label}/dash/settings/snapshot.json",
        note="the private VPS",
        error="HTTPStatusError: Client error '404 Not Found' for url 'http://127.0.0.1/x'",
    )


def _forward_down(label: str) -> PeerSnapshot:
    return PeerSnapshot(
        label=label,
        url=f"http://127.0.0.1/{label}",
        note="",
        error="ConnectError",
        tunnel_command=f"t3 peer up {label}",
    )


def _flat(text: str) -> str:
    """*text* with its line breaks collapsed — a wrapped sentence is still the same sentence."""
    return " ".join(text.split())


def _run(local: PeerSnapshot, peers: tuple[PeerSnapshot, ...], *args: str) -> Any:
    with (
        patch("teatree.core.settings.settings_compare.local_snapshot", return_value=local),
        patch("teatree.core.settings.settings_compare.peer_snapshots", return_value=peers),
    ):
        return runner.invoke(app, ["settings", "compare", *args], env={"COLUMNS": "110"})


@pytest.fixture
def three_legs() -> tuple[PeerSnapshot, tuple[PeerSnapshot, ...]]:
    """Two boxes that answer and one that never can — a fleet where a peer serves no dashboard."""
    local = _box("this instance", {"": {"merge_wip": 3}}, settings_key_count="287")
    peers = (
        _no_service("no-teatree-box"),
        _box("peer-box", {"": {"merge_wip": 1, "autonomy": "notify"}}, settings_key_count="277"),
    )
    return local, peers


class TestEveryConfiguredInstanceIsAccountedFor:
    def test_a_box_that_cannot_answer_is_named_with_its_reason(self, three_legs: Any) -> None:
        out = _flat(_run(*three_legs).stderr)
        assert "no-teatree-box" in out
        assert "no answer" in out
        assert "404 Not Found" in out

    def test_a_box_that_cannot_answer_is_not_an_error(self, three_legs: Any) -> None:
        assert _run(*three_legs).exit_code == 0

    def test_a_far_end_that_answered_is_not_told_to_open_a_tunnel(self, three_legs: Any) -> None:
        out = _flat(_run(*three_legs).stderr)
        assert "t3 peer up no-teatree-box" not in out
        assert "no tunnel this side opens changes it" in out

    def test_a_forward_that_never_carried_the_request_is_told_how_to_open_it(self) -> None:
        local = _box("this instance", {"": {"merge_wip": 3}})
        result = _run(local, (_forward_down("box-b"), _box("box-c", {"": {"merge_wip": 1}})))
        assert "t3 peer up box-b" in _flat(result.stderr)

    def test_the_caveat_is_repeated_over_the_rows_it_qualifies(self, three_legs: Any) -> None:
        assert "No reading below is from no-teatree-box." in _flat(_run(*three_legs).stderr)


class TestTheComparabilityVerdictReachesTheReaderFirst:
    def test_the_not_comparable_verdict_is_printed(self, three_legs: Any) -> None:
        assert "not comparable" in _flat(_run(*three_legs).stderr)

    def test_the_verdict_carries_the_caveat_the_diff_cannot_be_read_as_drift(self, three_legs: Any) -> None:
        assert "cannot be read as configuration drift" in _flat(_run(*three_legs).stderr)

    def test_the_verdict_comes_before_the_first_difference(self, three_legs: Any) -> None:
        out = _flat(_run(*three_legs).stderr)
        assert out.index("not comparable") < out.index("Differences")

    def test_comparable_boxes_carry_no_caveat(self) -> None:
        local = _box("this instance", {"": {"merge_wip": 3}}, settings_key_count="10")
        peer = _box("box-b", {"": {"merge_wip": 1}}, settings_key_count="10")
        out = _flat(_run(local, (peer,)).stderr)
        assert "not comparable" not in out
        assert "cannot be read as configuration drift" not in out


class TestDifferencesAreScannableWithoutABrowser:
    def test_values_that_disagree_are_grouped_first(self, three_legs: Any) -> None:
        out = _flat(_run(*three_legs).stderr)
        assert out.index("values differ") < out.index("override on one box")

    def test_each_group_states_how_many_rows_it_holds(self, three_legs: Any) -> None:
        assert "values differ (1)" in _flat(_run(*three_legs).stderr)

    def test_a_disagreeing_value_is_shown_under_the_box_that_holds_it(self, three_legs: Any) -> None:
        out = _run(*three_legs).stderr
        line = next(line for line in out.splitlines() if line.strip().startswith("merge_wip"))
        assert line.index("3") < line.index("1")

    def test_kind_selects_one_group_only(self, three_legs: Any) -> None:
        out = _flat(_run(*three_legs, "--kind", "values").stderr)
        assert "values differ" in out
        assert "override on one box (" not in out

    def test_an_unknown_kind_is_refused_rather_than_silently_ignored(self, three_legs: Any) -> None:
        result = _run(*three_legs, "--kind", "nonsense")
        assert result.exit_code == 2

    def test_full_prints_the_rule_that_decided_each_row(self, three_legs: Any) -> None:
        assert "if imported:" in _flat(_run(*three_legs, "--full").stderr)


class TestNothingToCompare:
    def test_one_answering_instance_exits_non_zero(self) -> None:
        result = _run(_box("this instance", {"": {"merge_wip": 3}}), (_no_service("no-teatree-box"),))
        assert result.exit_code == 1

    def test_the_peers_are_still_named_when_there_is_nothing_to_compare(self) -> None:
        out = _flat(_run(_box("this instance", {"": {"merge_wip": 3}}), (_no_service("no-teatree-box"),)).stderr)
        assert "no-teatree-box" in out
        assert "no reachable peer to compare against" in out


class TestTheJsonViewSummarisesNothingAway:
    def _payload(self, three_legs: Any) -> dict[str, Any]:
        return json.loads(_run(*three_legs, "--json").stdout)

    def test_every_configured_instance_is_present(self, three_legs: Any) -> None:
        labels = [instance["label"] for instance in self._payload(three_legs)["instances"]]
        assert labels == ["this instance", "no-teatree-box", "peer-box"]

    def test_the_instance_that_did_not_answer_carries_its_reason(self, three_legs: Any) -> None:
        unserved = next(one for one in self._payload(three_legs)["instances"] if one["label"] == "no-teatree-box")
        assert unserved["answered"] is False
        assert "404" in unserved["error"]

    def test_the_verdict_is_machine_readable(self, three_legs: Any) -> None:
        payload = self._payload(three_legs)
        assert payload["comparable"] is False
        assert payload["verdict"].startswith("not comparable")

    def test_a_row_names_the_value_each_box_holds(self, three_legs: Any) -> None:
        row = next(row for row in self._payload(three_legs)["rows"] if row["key"] == "merge_wip")
        assert [(cell["label"], cell["value"]) for cell in row["cells"]] == [
            ("this instance", 3),
            ("peer-box", 1),
        ]


class TestTheWaitIsTheOperatorsToChoose:
    def test_timeout_reaches_the_peer_fetch(self) -> None:
        local = _box("this instance", {"": {"merge_wip": 3}})
        with (
            patch("teatree.core.settings.settings_compare.local_snapshot", return_value=local),
            patch("teatree.core.settings.settings_compare.peer_snapshots", return_value=()) as peers,
        ):
            runner.invoke(app, ["settings", "compare", "--timeout", "30"], env={"COLUMNS": "110"})
        assert peers.call_args.args == (30.0,)
