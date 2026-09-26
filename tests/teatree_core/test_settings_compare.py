"""The comparison surface: the no-opinion rule, the compat verdict, and the write states.

The no-opinion rule is the correctness of the whole page. Getting it wrong does not produce a
wrong row — it produces HUNDREDS of them, one per key in a scope a box does not use, and one
per scope for every key a box's code lacks. So it is asserted as the property it is: three
different silences compare EQUAL, and only a stored value differs from silence.
"""

import json
import os
import pathlib
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

import teatree.core.settings.settings_compare as settings_compare_module
import teatree.dash
from teatree.config import PeerInstance, PeerTunnel
from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ValueSource
from teatree.config.setting_registries import CODE_PINNED_SETTINGS, ENV_VAR_BY_SETTING, SAFETY_POSTURE_KEYS
from teatree.core.models import ConfigSetting
from teatree.core.setting_control import SettingControl
from teatree.core.settings.settings_compare import (
    CONFIRM_GATED_REASON,
    WITHHELD_REASON,
    Cell,
    CompareRow,
    build_compare_view,
    reading_order,
)
from teatree.core.settings.settings_compare_rendering import FOLD_OVER, RowKind
from teatree.core.settings.settings_compare_sync import SEED, SETTING, SYNC_RULES, Disposition, classify
from teatree.core.settings.settings_compat import COMPAT_SIGNALS, Severity, build_compat_report
from teatree.core.settings.settings_peers import LOCAL_LABEL, PeerSnapshot, peer_snapshots, snapshot_url
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT, build_snapshot, canonical_json

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _row(*cells: Cell, **kwargs: Any) -> CompareRow:
    kwargs.setdefault("control", _control("merge_wip"))
    return CompareRow(surface=SETTING, scope="", title="merge_wip", subtitle="global scope", cells=cells, **kwargs)


def _control(key: str) -> SettingControl:
    """The control the page derives per key — the row half of every editable cell."""
    return SettingControl(key, shipped_defaults_table())


def _held(label: str, value: Any) -> Cell:
    return Cell(label=label, present=True, known=True, value=value)


def _no_row(label: str) -> Cell:
    """The box declares the key and simply holds no row for it."""
    return Cell(label=label, present=False, known=True)


def _not_declared(label: str) -> Cell:
    """The box's code does not carry the key at all, so it could never hold a row."""
    return Cell(label=label, present=False, known=False)


def _stale(label: str, value: Any) -> Cell:
    """A stored row for a key this box's code does not declare — a leftover from other code."""
    return Cell(label=label, present=True, known=False, value=value)


def _local(label: str, value: Any) -> Cell:
    """THIS box holding a value — the only cell the page offers a control for."""
    return Cell(label=label, present=True, known=True, value=value, local=True)


def _no_row_local(label: str) -> Cell:
    return Cell(label=label, present=False, known=True, local=True)


def _two_box_view() -> Any:
    """The real row builder over two boxes that agree on some keys and differ on others."""
    local = PeerSnapshot(
        label=LOCAL_LABEL,
        url="",
        note="",
        payload=_payload({"": {"merge_wip": True, "overlays": ["a"]}, "t3-teatree": {"merge_wip": False}}),
    )
    peer = _box("peer", {"": {"merge_wip": False, "overlays": ["a"]}})
    with (
        patch("teatree.core.settings.settings_compare.local_snapshot", return_value=local),
        patch("teatree.core.settings.settings_compare.peer_snapshots", return_value=(peer,)),
    ):
        return build_compare_view()


class TestTheNoOpinionRule(SimpleTestCase):
    def test_two_boxes_with_no_row_do_not_differ(self) -> None:
        assert not _row(_no_row("a"), _no_row("b")).differs

    def test_no_row_and_key_not_declared_are_the_same_silence(self) -> None:
        row = _row(_no_row("a"), _not_declared("b"))
        assert row.cells[0].opinion == row.cells[1].opinion
        assert not row.differs

    def test_every_silence_canonicalises_onto_one_sentinel(self) -> None:
        opinions = {cell.opinion for cell in (_no_row("a"), _not_declared("b"), _no_row("c"))}
        assert len(opinions) == 1

    def test_a_stored_value_differs_from_silence(self) -> None:
        assert _row(_held("a", 4), _no_row("b")).differs

    def test_a_stored_value_differs_from_a_box_that_cannot_declare_the_key(self) -> None:
        row = _row(_held("a", 4), _not_declared("b"))
        assert row.differs
        assert row.kind is RowKind.CODE

    def test_the_same_stored_value_on_both_boxes_does_not_differ(self) -> None:
        assert not _row(_held("a", [1, 2]), _held("b", [1, 2])).differs

    def test_two_different_stored_values_are_a_values_difference(self) -> None:
        assert _row(_held("a", 4), _held("b", 9)).kind is RowKind.VALUES

    def test_a_value_stored_on_one_declaring_box_only_is_an_override(self) -> None:
        assert _row(_held("a", 4), _no_row("b")).kind is RowKind.OVERRIDE


class TestAStaleRowStaysVisibleWithoutFabricatingADifference(SimpleTestCase):
    def test_a_row_stored_where_the_code_does_not_declare_it_is_shown(self) -> None:
        row = _row(_stale("a", "left over"), _no_row("b"))
        assert row.differs
        assert row.stale_on == ("a",)

    def test_it_is_classified_as_a_code_version_row_not_a_value_difference(self) -> None:
        assert _row(_stale("a", "left over"), _no_row("b")).kind is RowKind.CODE

    def test_a_stale_row_holds_no_opinion_so_it_never_counts_as_a_value(self) -> None:
        row = _row(_stale("a", "left over"), _no_row("b"))
        assert row.held == ()
        assert not row.opinions_differ

    def test_a_code_version_row_sorts_after_a_real_value_difference(self) -> None:
        values = _row(_held("a", 4), _held("b", 9))
        code = _row(_stale("a", "left over"), _no_row("b"))
        assert values.rank < code.rank


class TestWriteStates(SimpleTestCase):
    def test_the_rule_table_is_ordered_and_ends_in_a_catch_all(self) -> None:
        orders = [rule["order"] for rule in SYNC_RULES]
        assert orders == sorted(orders) == list(range(1, len(SYNC_RULES) + 1))
        assert SYNC_RULES[-1]["id"] == "differs"

    def test_a_differing_row_with_nothing_special_about_it_is_carried_by_an_import(self) -> None:
        outcome = classify(_row(_held("a", 4), _held("b", 9)))
        assert outcome.disposition is Disposition.IMPORT

    def test_a_value_with_no_toml_literal_is_blocked(self) -> None:
        assert classify(_row(_no_row("a"), _no_row("b"))).disposition is Disposition.BLOCKED

    def test_a_withheld_value_is_blocked_before_any_other_verdict(self) -> None:
        outcome = classify(_row(_held("a", 4), _held("b", 9), redacted=True))
        assert outcome.disposition is Disposition.BLOCKED
        assert outcome.rule == "secret-withheld"

    def test_a_secret_category_row_is_blocked_even_when_it_was_not_redacted(self) -> None:
        assert classify(_row(_held("a", 4), _held("b", 9), category="secret")).disposition is Disposition.BLOCKED

    def test_a_field_the_interchange_cannot_carry_is_manual(self) -> None:
        outcome = classify(_row(_held("a", 4), _held("b", 9), syncable=False))
        assert outcome.disposition is Disposition.MANUAL
        assert outcome.rule == "no-import-path"

    def test_a_value_equal_to_the_shipped_default_must_be_cleared_not_imported(self) -> None:
        outcome = classify(_row(_held("a", 4), _held("b", 9), equals_shipped_default=True))
        assert outcome.disposition is Disposition.CLEAR
        assert outcome.rule == "equals-default-setting"

    def test_an_env_shadowed_key_is_named_shadowed_because_the_import_would_change_nothing(self) -> None:
        outcome = classify(_row(_held("a", 4), _held("b", 9), env_shadowed=True))
        assert outcome.disposition is Disposition.SHADOWED
        assert outcome.rule == "env-shadowed"

    def test_the_clear_rule_wins_over_the_shadow_rule_because_it_is_ordered_first(self) -> None:
        row = _row(_held("a", 4), _held("b", 9), equals_shipped_default=True, env_shadowed=True)
        assert classify(row).rule == "equals-default-setting"

    def test_a_seed_row_absent_on_a_box_is_manual_because_there_is_no_row_to_update(self) -> None:
        row = CompareRow(
            surface=SEED,
            scope="loops",
            title="cadence_minutes",
            subtitle="loops.dream",
            cells=(_held("a", 30), _not_declared("b")),
        )
        assert classify(row).rule == "absent-on-target"

    def test_a_seed_row_equal_to_its_shipped_default_is_manual_not_clear(self) -> None:
        row = CompareRow(
            surface=SEED,
            scope="loops",
            title="cadence_minutes",
            subtitle="loops.dream",
            cells=(_held("a", 30), _held("b", 60)),
            equals_shipped_default=True,
        )
        outcome = classify(row)
        assert outcome.disposition is Disposition.MANUAL
        assert outcome.rule == "equals-default-seed"

    def test_a_rows_own_sync_note_replaces_the_generic_reason(self) -> None:
        row = _row(_held("a", 4), _held("b", 9), syncable=False, sync_note="tune it in defaults.toml")
        assert classify(row).reason == "tune it in defaults.toml"


class TestCompatSignals(SimpleTestCase):
    def _peer(self, label: str, **fingerprint: Any) -> PeerSnapshot:
        return PeerSnapshot(label=label, url="", note="", payload={"fingerprint": fingerprint})

    def test_the_two_schema_signals_are_the_only_blocking_ones(self) -> None:
        blocking = {signal.field for signal in COMPAT_SIGNALS if signal.severity is Severity.BLOCKING}
        assert blocking == {"settings_schema_sha256", "settings_key_count"}

    def test_the_applied_migration_count_is_never_an_input_to_the_verdict(self) -> None:
        signal = next(s for s in COMPAT_SIGNALS if s.field == "applied_migration_count")
        assert signal.severity is Severity.INFO
        assert "never an input to the verdict" in signal.note

    def test_agreeing_boxes_are_comparable(self) -> None:
        report = build_compat_report(
            [
                self._peer("a", settings_schema_sha256="ab", settings_key_count=3),
                self._peer("b", settings_schema_sha256="ab", settings_key_count=3),
            ]
        )
        assert report.comparable
        assert report.verdict == "comparable"

    def test_a_differing_schema_digest_blocks_the_comparison(self) -> None:
        report = build_compat_report(
            [self._peer("a", settings_schema_sha256="ab"), self._peer("b", settings_schema_sha256="cd")]
        )
        assert not report.comparable
        assert report.verdict.startswith("not comparable")

    def test_a_differing_info_signal_never_blocks(self) -> None:
        report = build_compat_report([self._peer("a", django_version="5.2"), self._peer("b", django_version="6.0")])
        assert report.comparable

    def test_a_differing_warn_signal_is_comparable_but_reported(self) -> None:
        report = build_compat_report(
            [self._peer("a", defaults_toml_sha256="ab"), self._peer("b", defaults_toml_sha256="cd")]
        )
        assert report.comparable
        assert report.warnings

    def test_a_signal_no_box_reported_is_a_failed_source_not_a_disagreement(self) -> None:
        report = build_compat_report([self._peer("a"), self._peer("b")])
        assert all(row.agrees for row in report.rows)

    def test_an_unreachable_peer_reports_nothing_rather_than_a_value(self) -> None:
        report = build_compat_report([self._peer("a", settings_key_count=3), PeerSnapshot("b", "", "", error="down")])
        row = next(row for row in report.rows if row.signal.field == "settings_key_count")
        assert row.readings == ("3", "")


class TestPeerFetching(SimpleTestCase):
    def test_the_snapshot_path_is_derived_from_the_urlconf_not_written_out(self) -> None:
        assert snapshot_url("http://127.0.0.1:9401") == "http://127.0.0.1:9401" + reverse("dash:settings_snapshot")

    def test_a_base_url_with_a_trailing_slash_resolves_the_same(self) -> None:
        assert snapshot_url("http://127.0.0.1:9401/") == snapshot_url("http://127.0.0.1:9401")


class TestTheEndpoints(TestCase):
    def test_the_snapshot_route_answers_a_snapshot(self) -> None:
        response = self.client.get(reverse("dash:settings_snapshot"), **_LOOPBACK)
        assert response.status_code == 200
        assert response.json()["format"] == SNAPSHOT_FORMAT

    def test_the_snapshot_route_is_read_only(self) -> None:
        assert self.client.post(reverse("dash:settings_snapshot"), **_LOOPBACK).status_code == 405

    def test_the_snapshot_route_is_refused_off_loopback(self) -> None:
        response = self.client.get(reverse("dash:settings_snapshot"), REMOTE_ADDR="10.0.0.9")
        assert response.status_code == 403

    def test_the_compare_page_renders_with_no_peer_configured(self) -> None:
        response = self.client.get(reverse("dash:settings_compare"), **_LOOPBACK)
        assert response.status_code == 200
        assert b"no reachable peer to compare against" in response.content

    def test_the_compare_page_changes_nothing_however_it_is_reached(self) -> None:
        """Its POST carries snapshot FILES to compare against — it is not a write path."""
        ConfigSetting.objects.set_value("merge_wip", 3, scope="")
        before = list(ConfigSetting.objects.values_list("scope", "key", "value"))
        assert self.client.post(reverse("dash:settings_compare"), **_LOOPBACK).status_code == 200
        assert list(ConfigSetting.objects.values_list("scope", "key", "value")) == before

    def test_the_compare_page_is_refused_off_loopback(self) -> None:
        assert self.client.get(reverse("dash:settings_compare"), REMOTE_ADDR="10.0.0.9").status_code == 403

    def test_the_settings_page_links_to_the_comparison(self) -> None:
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        assert reverse("dash:settings_compare").encode() in response.content


class TestOneUnresolvablePeerNeverTakesTheOthersDown(TestCase):
    """A peer whose own fields cannot be resolved is one degraded row, never a 500."""

    _PEERS = (
        PeerInstance(name="good", url="http://127.0.0.1:1/", tunnel=PeerTunnel(host="good.example.invalid")),
        PeerInstance(name="typo", url="http://127.0.0.1:94011/", tunnel=PeerTunnel(host="typo.example.invalid")),
    )

    def _snapshots(self) -> tuple[PeerSnapshot, ...]:
        with patch("teatree.core.settings.settings_peers.load_peer_instances", return_value=list(self._PEERS)):
            return peer_snapshots()

    def test_every_peer_still_gets_a_row(self) -> None:
        assert [peer.label for peer in self._snapshots()] == ["good", "typo"]

    def test_the_unresolvable_one_carries_its_reason(self) -> None:
        typo = self._snapshots()[1]
        assert not typo.reachable
        assert typo.error

    def test_the_compare_page_still_answers_200(self) -> None:
        with patch("teatree.core.settings.settings_peers.load_peer_instances", return_value=list(self._PEERS)):
            response = self.client.get(reverse("dash:settings_compare"), **_LOOPBACK)
        assert response.status_code == 200
        assert b"typo" in response.content


class TestUnreachablePeersAreNeverDropped(TestCase):
    def test_a_peer_that_cannot_be_fetched_is_listed_with_its_reason(self) -> None:
        unreachable = PeerSnapshot(label="box-b", url="http://127.0.0.1:1/x", note="", error="ConnectError")
        with patch("teatree.core.settings.settings_compare.peer_snapshots", return_value=(unreachable,)):
            view = build_compare_view()
        assert [instance.label for instance in view.instances] == ["this instance", "box-b"]
        assert view.unreachable == (unreachable,)
        assert view.error.startswith("no reachable peer to compare against")


class TestOneDeadPeerCostsExactlyOneRow(TestCase):
    """The unhappy path the page exists for, with nothing about the transport mocked.

    One peer's port genuinely has nothing listening on it and the other genuinely serves its
    snapshot over HTTP, because a guard exercised only against a patched ``httpx`` says nothing
    about the refusal an operator actually meets. What is asserted is the whole degradation
    contract: the dead peer costs its OWN row and only its own row — it is named, it carries
    its reason, it is kept out of the difference table's columns, and the peer that answered
    still produces the comparison it would have produced alone.

    The page is fetched ONCE for the class: it builds a full settings snapshot per request, so
    a fetch per assertion buys nothing and costs seconds. The assertions stay one fact each.
    """

    _KEY = "merge_wip"
    _LOCAL_VALUE = 3
    _PEER_VALUE = 41
    _DEAD = "unreachable-box"
    _LIVE = "answering-box"

    @classmethod
    def setUpClass(cls) -> None:
        # Not `setUpTestData`: what it publishes is deep-copied per test, and an HttpResponse
        # carries a ResolverMatch that refuses to be copied at all.
        super().setUpClass()
        ConfigSetting.objects.set_value(cls._KEY, cls._LOCAL_VALUE, scope="")
        peers = [
            PeerInstance(name=cls._DEAD, url=f"http://127.0.0.1:{cls._closed_port()}/"),
            PeerInstance(name=cls._LIVE, url=f"http://127.0.0.1:{cls._serve_snapshot()}/"),
        ]
        with (
            patch("teatree.core.settings.settings_peers.load_peer_instances", return_value=peers),
            # The FETCHING venue, pinned. Under CI this suite runs in a container, where
            # `fetch_target` rewrites a loopback peer onto the docker host — which cannot
            # reach a server bound on this container's own loopback, so the live peer would
            # read as refused. Pinning the venue leaves the transport itself untouched.
            patch("teatree.core.settings.settings_peers.host_published_port_host", return_value="127.0.0.1"),
        ):
            cls.response = Client().get(reverse("dash:settings_compare"), **_LOOPBACK)
        cls.view = cls.response.context["comparison"]

    @staticmethod
    def _closed_port() -> int:
        """A port with nothing on it — bound only to learn its number, then released."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    @classmethod
    def _serve_snapshot(cls) -> int:
        """Serve one real peer's snapshot on a real loopback port; return the port."""
        payload = build_snapshot(cls._LIVE)
        payload["values"]["settings"].setdefault("", {})[cls._KEY] = cls._PEER_VALUE
        body = json.dumps(payload).encode("utf-8")
        route = reverse("dash:settings_snapshot")

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != route:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's own name.
                """Silence: this test's output is its assertions, not an access log."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.addClassCleanup(server.server_close)
        cls.addClassCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return int(server.server_address[1])

    def test_the_page_still_answers(self) -> None:
        assert self.response.status_code == 200

    def test_the_dead_peer_is_named_on_the_page(self) -> None:
        assert self._DEAD.encode() in self.response.content

    def test_it_is_named_as_an_instance_that_did_not_answer_rather_than_dropped(self) -> None:
        assert [instance.label for instance in self.view.unreachable] == [self._DEAD]

    def test_the_dead_peer_carries_the_reason_it_did_not_answer(self) -> None:
        dead = self.view.unreachable[0]
        assert not dead.reachable
        assert dead.error

    def test_the_peer_that_answered_is_still_compared(self) -> None:
        assert self.view.labels == (LOCAL_LABEL, self._LIVE)

    def test_the_dead_peer_never_becomes_a_column(self) -> None:
        """A heading above no cell slides every later value under the wrong instance's name."""
        assert self._DEAD not in self.view.labels
        assert all(len(row.cells) == len(self.view.labels) for row in self.view.rows)

    def test_the_difference_the_reachable_peer_carries_is_still_reported(self) -> None:
        row = next(row for row in self.view.rows if row.title == self._KEY and not row.scope)
        assert [cell.value for cell in row.cells] == [self._LOCAL_VALUE, self._PEER_VALUE]

    def test_the_page_is_not_the_empty_one_it_shows_with_nothing_to_compare(self) -> None:
        assert not self.view.error
        assert b"no reachable peer to compare against" not in self.response.content

    def test_the_missing_box_is_reported_as_a_gap_not_as_a_failure(self) -> None:
        """A box that did not answer narrows the comparison; it does not break it.

        Rendered in the same red as the comparability verdict, this note competed with the one
        banner on the page that decides whether the rows below may be read as drift at all — and
        one of the two boxes here can NEVER answer, so its red was permanent.
        """
        body = self.response.content.decode()
        note = re.search(r'<p class="banner-amber">(.*?)</p>', body, re.DOTALL)
        assert note is not None
        assert self._DEAD in note.group(1)
        assert "banner-red" not in body


_DECLARED = ("merge_wip", "overlays")

_NESTED_STUB = {"__redacted__": "credential-coordinate", "sha256": "0" * 64}
_OTHER_NESTED_STUB = {"__redacted__": "credential-coordinate", "sha256": "1" * 64}


def _payload(stored: dict[str, dict[str, Any]], declares: tuple[str, ...] = _DECLARED) -> dict[str, Any]:
    """A snapshot payload carrying only what the row builder reads."""
    return {
        "format": SNAPSHOT_FORMAT,
        "fingerprint": {},
        "registry": {"settings": {key: {"syncable": True, "sync_note": "", "category": ""} for key in declares}},
        "values": {"settings": stored, "defaults": {}, "provenance": {}, "seed": {}, "seed_shipped": {}},
    }


def _box(label: str, stored: dict[str, dict[str, Any]], declares: tuple[str, ...] = _DECLARED) -> PeerSnapshot:
    return PeerSnapshot(label=label, url=f"http://127.0.0.1/{label}", note="", payload=_payload(stored, declares))


def _down(label: str) -> PeerSnapshot:
    return PeerSnapshot(label=label, url=f"http://127.0.0.1/{label}", note="", error="ConnectError")


class _RowBuilderCase(SimpleTestCase):
    """Drives the real row builder over crafted snapshots, so the assertions are the page's."""

    def _view(self, local: PeerSnapshot, *peers: PeerSnapshot) -> Any:
        with (
            patch.object(settings_compare_module, "local_snapshot", return_value=local),
            patch.object(settings_compare_module, "peer_snapshots", return_value=peers),
        ):
            return build_compare_view()


class TestSeedWriteEligibility(_RowBuilderCase):
    def _seed_box(
        self,
        label: str,
        *,
        stored: dict[str, dict[str, Any]],
        shipped: dict[str, dict[str, Any]],
    ) -> PeerSnapshot:
        payload = _payload({})
        payload["registry"]["seed"] = {
            "loops": {
                "fields": {
                    "description": {"syncable": True},
                    "delay_seconds": {"syncable": True},
                }
            }
        }
        payload["values"]["seed"] = {"loops": stored}
        payload["values"]["seed_shipped"] = {"loops": shipped}
        return PeerSnapshot(
            label=label,
            url="" if label == LOCAL_LABEL else f"http://127.0.0.1/{label}",
            note="",
            payload=payload,
        )

    def test_a_peer_only_seed_row_is_not_advertised_as_writable(self) -> None:
        local = self._seed_box(label=LOCAL_LABEL, stored={}, shipped={"review": {"description": ""}})
        peer = self._seed_box(
            label="peer",
            stored={"review": {"description": "there"}},
            shipped={"review": {"description": ""}},
        )

        row = next(row for row in self._view(local, peer).rows if row.subtitle == "loops.review")

        assert row.readings[0].cell is not None
        assert not row.readings[0].cell.writable
        assert "not present on this instance" in row.readings[0].cell.unwritable_reason

    def test_a_peer_only_owned_field_names_absence_before_its_dedicated_editor(self) -> None:
        local = self._seed_box(label=LOCAL_LABEL, stored={}, shipped={"review": {"delay_seconds": 300}})
        peer = self._seed_box(
            label="peer",
            stored={"review": {"delay_seconds": 60}},
            shipped={"review": {"delay_seconds": 300}},
        )

        row = next(row for row in self._view(local, peer).rows if row.subtitle == "loops.review")

        assert row.readings[0].cell is not None
        assert not row.readings[0].cell.writable
        assert "not present on this instance" in row.readings[0].cell.unwritable_reason
        assert "loops page" not in row.readings[0].cell.unwritable_reason

    def test_a_local_entry_not_carried_by_the_shipped_file_is_not_advertised_as_writable(self) -> None:
        local = self._seed_box(label=LOCAL_LABEL, stored={"custom": {"description": "here"}}, shipped={})
        peer = self._seed_box(label="peer", stored={"custom": {"description": "there"}}, shipped={})

        row = next(row for row in self._view(local, peer).rows if row.subtitle == "loops.custom")

        assert not row.readings[0].cell.writable
        assert "not carried by defaults.toml" in row.readings[0].cell.unwritable_reason


class TestKindFilteringPrecedesTheRenderCap(_RowBuilderCase):
    def test_a_late_code_row_is_not_lost_behind_the_global_cap(self) -> None:
        value_keys = tuple(f"value_{index:03d}" for index in range(500))
        code_key = "code_only"
        local = _box(
            LOCAL_LABEL,
            {"": {**dict.fromkeys(value_keys, "local"), code_key: "leftover"}},
            declares=(*value_keys, code_key),
        )
        peer = _box("peer", {"": dict.fromkeys(value_keys, "peer")}, declares=value_keys)
        with (
            patch.object(settings_compare_module, "local_snapshot", return_value=local),
            patch.object(settings_compare_module, "peer_snapshots", return_value=(peer,)),
        ):
            view = build_compare_view(kinds=(RowKind.CODE,))

        assert [row.title for row in view.rows] == [code_key]
        assert view.total_rows == 1
        assert view.differing_rows == 501
        assert view.compared_rows == 501
        assert view.identical == 0
        assert not view.truncated


class TestEveryColumnBelongsToTheBoxAboveIt(_RowBuilderCase):
    """Headings and cells must come from ONE sequence.

    Headings taken from the CONFIGURED instances, with cells built only from the ones that
    ANSWERED, shifts every value one column left of the box that holds it the moment a peer in
    the middle goes down — and a shifted table reads as a confident, wrong answer.
    """

    def _shifted(self) -> Any:
        return self._view(
            _box("this instance", {"": {"merge_wip": "local-value"}}),
            _down("box-b"),
            _box("box-c", {"": {"merge_wip": "c-value"}}),
        )

    def test_no_value_is_rendered_under_another_boxs_heading(self) -> None:
        view = self._shifted()
        for row in view.rows:
            assert [cell.label for cell in row.cells] == list(view.labels)

    def test_the_headings_are_the_boxes_that_answered(self) -> None:
        assert self._shifted().labels == ("this instance", "box-c")

    def test_the_peer_that_did_not_answer_is_still_listed_with_its_reason(self) -> None:
        view = self._shifted()
        assert [instance.label for instance in view.instances] == ["this instance", "box-b", "box-c"]
        assert [instance.label for instance in view.unreachable] == ["box-b"]


class TestTheRowBuilderKeepsTheNoOpinionRule(_RowBuilderCase):
    def test_two_boxes_holding_the_same_value_produce_no_row(self) -> None:
        view = self._view(
            _box("this instance", {"": {"merge_wip": True}}),
            _box("box-b", {"": {"merge_wip": True}}),
        )
        assert view.rows == ()

    def test_two_boxes_holding_different_values_produce_one_values_row(self) -> None:
        view = self._view(
            _box("this instance", {"": {"merge_wip": True}}),
            _box("box-b", {"": {"merge_wip": False}}),
        )
        assert [(row.title, row.kind) for row in view.rows] == [("merge_wip", RowKind.VALUES)]

    def test_a_scope_one_box_never_uses_is_not_a_difference_per_key(self) -> None:
        view = self._view(
            _box("this instance", {"": {"merge_wip": True}, "solo": {"merge_wip": True}}),
            _box("box-b", {"": {"merge_wip": True}}),
        )
        assert [row.scope for row in view.rows] == ["solo"]

    def test_a_key_one_box_does_not_declare_is_a_code_version_row_not_a_value_difference(self) -> None:
        view = self._view(
            _box("this instance", {"": {"merge_wip": True}}),
            _box("box-b", {"": {}}, declares=("overlays",)),
        )
        assert [(row.title, row.kind) for row in view.rows] == [("merge_wip", RowKind.CODE)]


class TestAWithheldValueIsBlockedHoweverDeepItSits(_RowBuilderCase):
    """The stub the capture leaves behind must block the import wherever inside the row it sits.

    A withheld leaf inside an innocuous row is exactly the shape the capture now produces for
    the ``overlays`` registry, and reading only the row's top level calls it an ordinary
    difference — so the page offers to import a value that is a redaction stub.
    """

    def _classified(self, local_value: Any, peer_value: Any) -> Any:
        view = self._view(
            _box("this instance", {"": {"overlays": local_value}}),
            _box("box-b", {"": {"overlays": peer_value}}),
        )
        return next(row for row in view.rows if row.title == "overlays").outcome

    def test_a_whole_row_replaced_by_a_stub_is_blocked(self) -> None:
        outcome = self._classified(_NESTED_STUB, _OTHER_NESTED_STUB)
        assert outcome.disposition is Disposition.BLOCKED
        assert outcome.rule == "secret-withheld"

    def test_a_stub_nested_inside_the_row_is_blocked_too(self) -> None:
        outcome = self._classified(
            {"box": {"messaging_backend": "slack", "slack_token_ref": _NESTED_STUB}},
            {"box": {"messaging_backend": "slack", "slack_token_ref": _OTHER_NESTED_STUB}},
        )
        assert outcome.disposition is Disposition.BLOCKED
        assert outcome.rule == "secret-withheld"

    def test_a_stub_nested_inside_a_list_is_blocked_too(self) -> None:
        outcome = self._classified({"box": [_NESTED_STUB]}, {"box": [_OTHER_NESTED_STUB]})
        assert outcome.disposition is Disposition.BLOCKED

    def test_a_row_carrying_no_stub_at_any_depth_is_still_importable(self) -> None:
        outcome = self._classified({"box": {"messaging_backend": "slack"}}, {"box": {"messaging_backend": "teams"}})
        assert outcome.disposition is Disposition.IMPORT


class TestTheDifferencesArriveGroupedByScope(_RowBuilderCase):
    """One scope's differences arrive together, because that is the question a reader asks.

    Ordered by the FIELD first, a seed field shared by twenty loops produces twenty consecutive
    rows whose visible name is identical and whose distinguishing scope is scattered down the
    column — ninety-six rows with no structure to scan. The scope is what makes a block.
    """

    _TABLE = "loops"
    _FIELDS = ("cadence_minutes", "default_enabled")
    _ENTITIES = ("dream", "review")

    def _seed_box(self, label: str, values: dict[str, Any]) -> PeerSnapshot:
        payload = _payload({})
        payload["registry"]["seed"] = {self._TABLE: {"fields": {field: {} for field in self._FIELDS}}}
        payload["values"]["seed"] = {self._TABLE: {name: dict(values) for name in self._ENTITIES}}
        return PeerSnapshot(label=label, url=f"http://127.0.0.1/{label}", note="", payload=payload)

    def _both_boxes(self) -> Any:
        return self._view(
            self._seed_box("this instance", {"cadence_minutes": 30, "default_enabled": True}),
            self._seed_box("box-b", {"cadence_minutes": 60, "default_enabled": False}),
        )

    def test_every_row_of_one_scope_is_contiguous(self) -> None:
        subtitles = [row.subtitle for row in self._both_boxes().rows]
        assert subtitles == ["loops.dream", "loops.dream", "loops.review", "loops.review"]

    def test_the_fields_inside_a_scope_stay_in_their_own_order(self) -> None:
        dream = [row.title for row in self._both_boxes().rows if row.subtitle == "loops.dream"]
        assert dream == list(self._FIELDS)


class TestEveryClassThePageEmitsIsStyled(SimpleTestCase):
    """The page's tables were unreadable because their class matched NO rule in any stylesheet.

    ``class="dash-table"`` is not the dash's table class — the dash's is ``dash`` — so all three
    tables rendered with the browser's defaults: no width, no cell padding, no row rules, no
    header treatment. Worse, a table with no width measures its own content, and one seed field
    holds a 500-character single-token JSON object whose token has no break opportunity. That one
    token set the min-content width of its column: the table came out 8567px wide inside a 1408px
    band and the second box's column began at x=4203 — off-screen for every one of the ninety-six
    rows, on a page whose whole purpose is reading two columns against each other.

    A stale class name is invisible: nothing errors, the page just stops looking like the dash.
    So the invariant is asserted directly — every class the template writes has a rule behind it.
    """

    #: Stands in for a ``{{ … }}`` so a token BUILT from one is recognisable and skipped: the
    #: scan cannot know what ``sev-{{ row.severity }}`` renders to, and ``sev-`` is not a class.
    _INTERPOLATED = "~interpolated~"

    def _dash_dir(self) -> pathlib.Path:
        return pathlib.Path(teatree.dash.__file__).parent

    def _stylesheets(self) -> str:
        sheets = sorted((self._dash_dir() / "static" / "dash" / "css").glob("*.css"))
        return "\n\n".join(sheet.read_text() for sheet in sheets)

    def _template(self) -> str:
        return (self._dash_dir() / "templates" / "dash" / "settings_compare.html").read_text()

    def _emitted(self) -> set[str]:
        """Every literal class token in the template, minus the ones a template tag interpolates."""
        tokens: set[str] = set()
        for attribute in re.findall(r'class="([^"]*)"', self._template()):
            literal = re.sub(r"\{%.*?%\}", " ", attribute, flags=re.DOTALL)
            tokens.update(re.sub(r"\{\{.*?\}\}", self._INTERPOLATED, literal, flags=re.DOTALL).split())
        return {token for token in tokens if self._INTERPOLATED not in token}

    def test_the_scan_can_read_the_template_it_asserts_over(self) -> None:
        """Anti-vacuity: a template this could not parse would pass every assertion below."""
        assert len(self._emitted()) > 20

    def test_every_emitted_class_has_a_rule_behind_it(self) -> None:
        css = self._stylesheets()
        unstyled = sorted(token for token in self._emitted() if not re.search(rf"\.{re.escape(token)}(?![\w-])", css))
        assert unstyled == []

    def test_the_two_interpolated_severity_classes_are_styled(self) -> None:
        """``sev-{{ row.severity }}`` is invisible to the scan above, so it is named here."""
        css = self._stylesheets()
        assert ".compare-table tr.sev-blocking" in css
        assert ".compare-table tr.sev-warn" in css

    def test_all_three_tables_carry_the_dash_table_class(self) -> None:
        template = self._template()
        assert template.count('<table class="dash ') == 3
        assert "dash-table" not in template


class TestARowRendersOneReadingPerInstance(SimpleTestCase):
    """Every presentation decision is made in `readings`, so the template holds no judgement."""

    def test_a_digest_reads_as_a_verdict_and_never_as_hex(self) -> None:
        stub = {"__redacted__": "private-key", "sha256": "a" * 64}
        other = {"__redacted__": "private-key", "sha256": "b" * 64}
        row = _row(_held("a", stub), _held("b", other), redacted=True)
        rendered = [reading.text for reading in row.readings]
        assert rendered == ["same", "differs"]
        assert not any("a" * 64 in text or "sha256" in text for text in rendered)

    def test_two_equal_digests_both_read_same(self) -> None:
        stub = {"__redacted__": "private-key", "sha256": "c" * 64}
        row = _row(_held("a", stub), _held("b", stub), _no_row("c"), redacted=True)
        assert [reading.text for reading in row.readings] == ["same", "same", "— absent —"]

    def test_a_long_value_folds_to_one_line_and_keeps_the_whole(self) -> None:
        payload = {f"loop_{index}": False for index in range(40)}
        reading = _row(_held("a", payload), _no_row("b")).readings[0]
        assert reading.folded
        assert len(reading.brief) <= FOLD_OVER
        assert reading.brief.endswith("…")
        assert reading.text == canonical_json(payload)

    def test_a_short_value_is_not_folded(self) -> None:
        reading = _row(_held("a", value=True), _no_row("b")).readings[0]
        assert not reading.folded
        assert reading.brief == reading.text == "true"

    def test_a_silent_cell_says_absent_and_never_carries_a_verdict(self) -> None:
        reading = _row(_held("a", 1), _no_row("b")).readings[1]
        assert reading.text == "— absent —"
        assert not reading.present


class TestOnlyThisBoxIsEditable(SimpleTestCase):
    """The owner edits what differs on THIS box; a peer's reading stays a reading.

    Every assertion reads through ``reading.cell`` — the same
    :class:`~teatree.core.setting_cell.SettingCell` the single-instance grid edits through — so
    a second derivation of "how is this key written" cannot reappear on this page.
    """

    def test_this_box_gets_a_control_and_a_peer_does_not(self) -> None:
        row = _row(_local("this instance", value=False), _held("peer", value=True))
        assert [reading.cell is not None for reading in row.readings] == [True, False]
        assert row.readings[0].cell.writable

    def test_the_control_posts_the_key_in_the_path_and_the_scope_in_the_query(self) -> None:
        row = CompareRow(
            surface=SETTING,
            scope="t3-teatree",
            title="merge_wip",
            subtitle="overlay scope t3-teatree",
            cells=(_local("this instance", value=False),),
            control=_control("merge_wip"),
        )
        assert row.readings[0].cell.post_url == f"{reverse('dash:settings_set', args=['merge_wip'])}?scope=t3-teatree"

    def test_a_global_scope_control_posts_no_scope_at_all(self) -> None:
        assert "?" not in _row(_local("this instance", value=False)).readings[0].cell.post_url

    def test_a_seed_field_outside_the_interchange_is_never_editable_here(self) -> None:
        # `enabled` is an incident state, deliberately excluded from `SEED_ROW_FIELDS`: there
        # is no row for a write to land on, so the page offers no control for it.
        row = CompareRow(
            surface=SEED,
            scope="loops",
            title="enabled",
            subtitle="loops.dispatch",
            cells=(_local("this instance", value=False), _held("peer", value=True)),
        )
        assert not any(reading.cell for reading in row.readings)

    def test_a_key_the_schema_does_not_know_is_never_editable(self) -> None:
        row = CompareRow(
            surface=SETTING,
            scope="",
            title="a_key_no_teatree_declares",
            subtitle="global scope",
            cells=(_local("this instance", value=False), _held("peer", value=True)),
        )
        assert not any(reading.cell for reading in row.readings)

    def test_a_withheld_value_is_never_editable_from_a_page_that_cannot_show_it(self) -> None:
        row = _row(_local("this instance", "x"), _held("peer", "y"), redacted=True)
        assert not any(reading.cell and reading.cell.writable for reading in row.readings)
        assert row.readings[0].cell.unwritable_reason == WITHHELD_REASON

    def test_a_safety_posture_key_keeps_its_confirm_gate_and_is_not_edited_here(self) -> None:
        key = next(iter(SAFETY_POSTURE_KEYS))
        row = CompareRow(
            surface=SETTING,
            scope="",
            title=key,
            subtitle="global scope",
            cells=(_local("this instance", value=False), _held("peer", value=True)),
            control=_control(key),
        )
        assert not any(reading.cell and reading.cell.writable for reading in row.readings)
        assert row.readings[0].cell.unwritable_reason == CONFIRM_GATED_REASON

    def test_a_key_an_env_var_pins_is_refused_here_exactly_as_on_the_settings_grid(self) -> None:
        key, env_var = next(iter(ENV_VAR_BY_SETTING.items()))
        row = CompareRow(
            surface=SETTING,
            scope="",
            title=key,
            subtitle="global scope",
            cells=(_local("this instance", value=False), _held("peer", value=True)),
            control=_control(key),
            source=ValueSource.ENV.value,
        )
        with patch.dict(os.environ, {env_var: "1"}):
            assert not row.readings[0].cell.writable
            assert row.readings[0].cell.unwritable_reason == f"pinned by {env_var}"

    def test_a_key_pinned_to_its_shipped_value_in_code_is_refused_with_the_reason(self) -> None:
        """The two destructive levers render read-only, not as an edit box over a dead write.

        The scanner reads them off `UserSettings()`, so a stored row changes no behaviour —
        and the cell then re-renders as DRIFTED, showing the operator their own write echoed
        back by a mechanism that ignores it. That is worse than refusing the edit.
        """
        for key, reason in CODE_PINNED_SETTINGS.items():
            row = CompareRow(
                surface=SETTING,
                scope="",
                title=key,
                subtitle="global scope",
                cells=(_local("this instance", value=False), _held("peer", value=True)),
                control=_control(key),
            )

            assert not row.readings[0].cell.writable, key
            assert row.readings[0].cell.unwritable_reason == reason, key

    def test_an_ordinary_key_stays_writable_so_the_refusal_is_not_blanket(self) -> None:
        # The control for the assertion above: without it a refusal that fired on every key
        # would read exactly the same.
        row = _row(_local("this instance", value=False), _held("peer", value=True))

        assert row.readings[0].cell.writable
        assert row.readings[0].cell.unwritable_reason == ""

    def test_a_control_holds_the_wire_literal_so_what_is_shown_is_what_would_be_posted(self) -> None:
        row = _row(_local("this instance", value=False), _held("peer", value=True))
        assert row.readings[0].cell.editable == "false"

    def test_an_unset_control_is_empty_rather_than_the_four_letters_null(self) -> None:
        row = _row(_no_row_local("this instance"), _held("peer", value=True))
        assert row.readings[0].cell.editable == ""
        assert row.readings[0].cell.selected == "null"


class TestThisBoxsSeedRowsAreEditableToo(SimpleTestCase):
    """A loop / preset / schedule field the interchange carries is edited HERE, like a setting.

    The page listed every seed difference and offered a control for none of them, so the one
    surface that shows a loop's description differing between two boxes could not change it —
    and for five of the nine interchange seed fields no other dash surface can either.

    A field a DEDICATED editor owns stays a reading and names that editor: a second writer for
    a cadence pair or a preset's entries table would bypass the invariant its own editor
    repairs.
    """

    def _seed(self, table: str, name: str, field: str, **kwargs: Any) -> CompareRow:
        return CompareRow(
            surface=SEED,
            scope=table,
            title=field,
            subtitle=f"{table}.{name}",
            entry=name,
            cells=(_local("this instance", "here"), _held("peer", "there")),
            seed_shipped=True,
            **kwargs,
        )

    def test_a_loops_description_is_editable_on_this_box(self) -> None:
        row = self._seed("loops", "review", "description")
        assert row.readings[0].cell is not None
        assert row.readings[0].cell.writable

    def test_a_peers_seed_cell_is_still_only_a_reading(self) -> None:
        assert self._seed("loops", "review", "description").readings[1].cell is None

    def test_the_control_posts_the_table_entry_and_field_it_names(self) -> None:
        url = self._seed("loops", "review", "description").readings[0].cell.post_url
        assert url == reverse("dash:settings_seed_set", args=["loops", "review", "description"])

    def test_a_preset_description_and_a_schedule_timezone_are_editable_too(self) -> None:
        assert self._seed("modes", "afk", "description").readings[0].cell.writable
        assert self._seed("schedules", "week", "timezone").readings[0].cell.writable

    def test_a_cadence_field_stays_a_reading_and_names_the_editor_that_owns_it(self) -> None:
        cell = self._seed("loops", "review", "delay_seconds").readings[0].cell
        assert not cell.writable
        assert "loops page" in cell.unwritable_reason

    def test_a_presets_entries_table_stays_a_reading_and_names_its_editor(self) -> None:
        cell = self._seed("modes", "afk", "entries").readings[0].cell
        assert not cell.writable
        assert "preset" in cell.unwritable_reason

    def test_a_withheld_seed_value_is_never_editable_from_a_page_that_hid_it(self) -> None:
        row = self._seed("loops", "review", "description", redacted=True)
        assert not row.readings[0].cell.writable


class TestTheSeedControlReachesTheRenderedPage(TestCase):
    """The control is asserted on the PAGE, not on the row object that would produce one.

    A cell can be `writable` and still render as text: the template forks on the cell, and a
    seed row carries no `SettingControl`, so the branch it takes is not the one the settings
    grid takes. What the operator gets is the markup.
    """

    _TABLE = "loops"
    _ENTRY = "review"
    _FIELD = "description"

    def _body(self) -> str:
        local = PeerSnapshot(label=LOCAL_LABEL, url="", note="", payload=self._payload("here", 300))
        peer = PeerSnapshot(label="peer", url="http://127.0.0.1/peer", note="", payload=self._payload("there", 60))
        with (
            patch("teatree.core.settings.settings_compare.local_snapshot", return_value=local),
            patch("teatree.core.settings.settings_compare.peer_snapshots", return_value=(peer,)),
        ):
            return self.client.get(reverse("dash:settings_compare"), **_LOOPBACK).content.decode()

    def _payload(self, description: str, delay_seconds: int) -> dict[str, Any]:
        payload = _payload({})
        payload["registry"]["seed"] = {
            self._TABLE: {"fields": {self._FIELD: {"syncable": True}, "delay_seconds": {"syncable": True}}}
        }
        payload["values"]["seed"] = {
            self._TABLE: {self._ENTRY: {self._FIELD: description, "delay_seconds": delay_seconds}}
        }
        payload["values"]["seed_shipped"] = {self._TABLE: {self._ENTRY: {self._FIELD: "", "delay_seconds": 300}}}
        return payload

    def test_the_editable_seed_cell_renders_a_control_posting_to_the_seed_endpoint(self) -> None:
        url = reverse("dash:settings_seed_set", args=[self._TABLE, self._ENTRY, self._FIELD])
        assert f'hx-post="{url}"' in self._body()

    def test_a_field_a_dedicated_editor_owns_renders_its_reason_instead_of_a_control(self) -> None:
        body = self._body()
        cadence = reverse("dash:settings_seed_set", args=[self._TABLE, self._ENTRY, "delay_seconds"])
        assert f'hx-post="{cadence}"' not in body
        assert "cadence is edited on the loops page" in body

    def test_a_seed_refusal_does_not_link_to_the_unrelated_settings_editor(self) -> None:
        body = self._body()
        start = body.index("cadence is edited on the loops page")
        note = body[start : body.index("</span>", start)]
        assert reverse("dash:settings") not in note


class TestNothingLooksHidden(TestCase):
    """A page that shows only differences must still say how many rows it did not show."""

    def test_the_view_counts_the_rows_that_compared_equal(self) -> None:
        view = _two_box_view()
        assert view.identical > 0
        assert view.identical == view.compared_rows - view.total_rows

    def test_the_rows_it_shows_are_the_ones_that_differ(self) -> None:
        assert all(row.differs for row in _two_box_view().rows)

    def test_the_groups_partition_the_shown_rows_exactly_once(self) -> None:
        view = _two_box_view()
        grouped = [row for group in view.groups for row in group.rows]
        assert len(grouped) == len(view.rows)
        assert {id(row) for row in grouped} == {id(row) for row in view.rows}

    def test_the_conflicting_group_leads_and_opens(self) -> None:
        groups = _two_box_view().groups
        assert groups[0].kind is RowKind.VALUES
        assert groups[0].open
        assert not any(group.open for group in groups[1:])

    def test_every_group_carries_its_own_count(self) -> None:
        for group in _two_box_view().groups:
            assert group.count == len(group.rows)


class TestTheActionableRowsLead(SimpleTestCase):
    """A page for editing what differs must not open on fifty rows it cannot offer a control for."""

    def _mixed(self) -> list[CompareRow]:
        return [
            CompareRow(
                surface=SEED,
                scope="loops",
                title="default_enabled",
                subtitle=f"loops.{name}",
                cells=(_local("a", value=True), _held("b", value=False)),
            )
            for name in ("audit", "dogfood", "dream")
        ] + [_row(_local("a", value), _held("b", not value)) for value in (True, False)]

    def test_every_row_this_box_can_change_sorts_above_every_row_it_cannot(self) -> None:
        ordered = sorted(self._mixed(), key=reading_order)
        writable = [index for index, row in enumerate(ordered) if row.writable]
        readonly = [index for index, row in enumerate(ordered) if not row.writable]
        assert writable
        assert readonly
        assert max(writable) < min(readonly)

    def test_a_seed_field_never_outranks_an_editable_setting(self) -> None:
        seed = CompareRow(
            surface=SEED,
            scope="loops",
            title="default_enabled",
            subtitle="loops.audit",
            cells=(_local("a", value=True), _held("b", value=False)),
        )
        setting = _row(_local("a", value=True), _held("b", value=False))
        assert min([seed, setting], key=reading_order) is setting


class TestTheControlOnThisPageActuallyWrites(TestCase):
    """The acceptance test: a value the boxes disagree on is CHANGED from the compare page.

    Asserted end to end against the URL the page itself renders, because a control that posts to
    a URL nothing serves looks exactly like one that works until an operator tries it.
    """

    _KEY = "dashboard_instance_label"
    _NEW = "compare-page-write-probe"

    def _row(self, scope: str = "") -> CompareRow:
        return CompareRow(
            surface=SETTING,
            scope=scope,
            title=self._KEY,
            subtitle="global scope",
            cells=(_local(LOCAL_LABEL, "hostkey"), _held("peer", "mac")),
            control=_control(self._KEY),
        )

    def test_posting_the_rendered_url_stores_the_value(self) -> None:
        url = self._row().readings[0].cell.post_url
        response = Client().post(url, {"value": json.dumps(self._NEW)}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        assert response.status_code == 200
        assert ConfigSetting.objects.get(key=self._KEY, scope="").value == self._NEW

    def test_posting_a_scoped_control_stores_it_in_that_scope_only(self) -> None:
        url = self._row(scope="t3-teatree").readings[0].cell.post_url
        Client().post(url, {"value": json.dumps(self._NEW)}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        assert ConfigSetting.objects.filter(key=self._KEY, scope="t3-teatree").exists()
        assert not ConfigSetting.objects.filter(key=self._KEY, scope="").exists()

    def test_an_emptied_control_clears_the_row_so_the_default_resolves_again(self) -> None:
        url = self._row().readings[0].cell.post_url
        Client().post(url, {"value": json.dumps(self._NEW)}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        Client().post(url, {"value": ""}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        assert not ConfigSetting.objects.filter(key=self._KEY, scope="").exists()

    def test_a_refused_value_answers_400_and_stores_nothing(self) -> None:
        url = self._row().readings[0].cell.post_url
        response = Client().post(url, {"value": "{not json"}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        assert response.status_code == 400
        assert not ConfigSetting.objects.filter(key=self._KEY, scope="").exists()


class TestTheShippedColumnIsOnTheCompareGrid(TestCase):
    """`defaults.toml` reads as a COLUMN here, beside the boxes it is being compared to.

    D1's question is "does the shipped file ship perfect defaults, and what diverges from it
    across the three factories" — which needs the shipped value in the same row as each box's,
    not on a second page. It belongs HERE and not on the settings grid: that grid edits ONE
    box, where the shipped default is already the row's own reference and a column would just
    repeat it.

    The grid renders only against something to compare to, which needs a reachable peer this
    suite has none of — so the column is asserted on the template that emits it, as the
    sibling stylesheet invariant in this file already is.
    """

    def _template(self) -> str:
        return (pathlib.Path(teatree.dash.__file__).parent / "templates" / "dash" / "settings_compare.html").read_text()

    def test_the_grid_header_carries_a_shipped_column(self) -> None:
        assert '<th class="col-shipped">defaults.toml</th>' in self._template()

    def test_the_shipped_cell_is_read_only(self) -> None:
        # It is what SHIPS, not a value on any box — an editable control here would be
        # editing a file through a page whose job is comparing boxes.
        cell = next(line for line in self._template().splitlines() if "compare-cell shipped" in line)
        assert "hx-post" not in cell
        assert "<input" not in cell
        assert "<select" not in cell

    def test_the_settings_grid_does_not_grow_a_shipped_column(self) -> None:
        # The control: the owner's ruling is that this is a compare-page marker, never a
        # settings-page one, so the removal direction is asserted against the live page.
        settings_page = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert "defaults.toml" not in settings_page
