"""The comparison surface: the no-opinion rule, the compat verdict, and the write states.

The no-opinion rule is the correctness of the whole page. Getting it wrong does not produce a
wrong row — it produces HUNDREDS of them, one per key in a scope a box does not use, and one
per scope for every key a box's code lacks. So it is asserted as the property it is: three
different silences compare EQUAL, and only a stored value differs from silence.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from teatree.config import PeerInstance, PeerTunnel
from teatree.core.models import ConfigSetting
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT, build_snapshot
from teatree.dash.settings_compare import (
    SEED,
    SETTING,
    SYNC_RULES,
    Cell,
    CompareRow,
    Disposition,
    RowKind,
    build_compare_view,
    classify,
)
from teatree.dash.settings_compat import COMPAT_SIGNALS, Severity, build_compat_report
from teatree.dash.settings_peers import LOCAL_LABEL, PeerSnapshot, peer_snapshots, snapshot_url

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _row(*cells: Cell, **kwargs: Any) -> CompareRow:
    return CompareRow(surface=SETTING, scope="", title="merge_wip", subtitle="global scope", cells=cells, **kwargs)


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
        with patch("teatree.dash.settings_peers.load_peer_instances", return_value=list(self._PEERS)):
            return peer_snapshots()

    def test_every_peer_still_gets_a_row(self) -> None:
        assert [peer.label for peer in self._snapshots()] == ["good", "typo"]

    def test_the_unresolvable_one_carries_its_reason(self) -> None:
        typo = self._snapshots()[1]
        assert not typo.reachable
        assert typo.error

    def test_the_compare_page_still_answers_200(self) -> None:
        with patch("teatree.dash.settings_peers.load_peer_instances", return_value=list(self._PEERS)):
            response = self.client.get(reverse("dash:settings_compare"), **_LOOPBACK)
        assert response.status_code == 200
        assert b"typo" in response.content


class TestUnreachablePeersAreNeverDropped(TestCase):
    def test_a_peer_that_cannot_be_fetched_is_listed_with_its_reason(self) -> None:
        unreachable = PeerSnapshot(label="box-b", url="http://127.0.0.1:1/x", note="", error="ConnectError")
        with patch("teatree.dash.settings_compare.peer_snapshots", return_value=(unreachable,)):
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
            patch("teatree.dash.settings_peers.load_peer_instances", return_value=peers),
            # The FETCHING venue, pinned. Under CI this suite runs in a container, where
            # `fetch_target` rewrites a loopback peer onto the docker host — which cannot
            # reach a server bound on this container's own loopback, so the live peer would
            # read as refused. Pinning the venue leaves the transport itself untouched.
            patch("teatree.dash.settings_peers.host_published_port_host", return_value="127.0.0.1"),
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
            patch("teatree.dash.settings_compare.local_snapshot", return_value=local),
            patch("teatree.dash.settings_compare.peer_snapshots", return_value=peers),
        ):
            return build_compare_view()


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
