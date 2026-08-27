"""Loading a saved snapshot into the comparison, and saving this box's own as a file.

The live fetch needs the peer to be UP, so three columns were unreachable from the dashboard:
a box whose tunnel is down, a box that is gone, and this box as it stood weeks ago. A file
supplies all three — which only works if a RECORD is diffed by the same rules as a live peer
and can never be read as a live value.

Two properties carry that and are asserted as properties rather than as examples:

*   an OLDER record, whose document legitimately lacks what today's code declares, produces
    no fabricated difference — the silence rule that keeps one missing key from reading as one
    difference per scope, and one missing scope from reading as one difference per key; and
*   a record never decides whether the LIVE boxes may be compared, because a schema skew
    between "then" and "now" is the record's AGE, not two boxes running different code.
"""

import copy
import json
from typing import Any
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting
from teatree.core.settings_snapshot import FORMAT_VERSION, SNAPSHOT_FORMAT, build_snapshot
from teatree.dash.settings_compare import RowKind, build_compare_view
from teatree.dash.settings_compat import Severity, build_compat_report
from teatree.dash.settings_files import MAX_SNAPSHOT_BYTES, LoadRefusal, load_snapshots, snapshot_filename
from teatree.dash.settings_peers import PeerSnapshot, SnapshotOrigin

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

#: A setting the fixtures never store, so dropping it from a record adds nothing to either side.
_UNSTORED_KEY = "merge_wip"

#: A setting the fixtures DO store, in a scope the older record is made never to have used.
_STORED_KEY = "admin_autologin_enabled"
_STORED_SCOPE = "solo"


def _document(**overrides: Any) -> dict[str, Any]:
    payload = {
        "format": SNAPSHOT_FORMAT,
        "format_version": FORMAT_VERSION,
        "captured_at": "2026-07-30T09:12:00Z",
        "instance": {"label": "the vps", "note": "decommissioned"},
        "fingerprint": {},
        "registry": {"settings": {}},
        "values": {"settings": {}, "defaults": {}, "provenance": {}, "seed": {}, "seed_shipped": {}},
    }
    payload.update(overrides)
    return payload


def _raw(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _refusal(source: str, raw: bytes) -> LoadRefusal:
    loaded = load_snapshots([(source, raw)])
    assert loaded.snapshots == (), "that document should not have loaded"
    return loaded.refusals[0]


class TestARefusedDocumentIsNamedNotIgnored(SimpleTestCase):
    """A file dropped in silence reads as a box that agrees with everything."""

    def test_a_document_that_is_not_json_is_refused_with_the_parser_error(self) -> None:
        assert "not valid JSON" in _refusal("notes.txt", b"not json at all").reason

    def test_a_json_document_that_is_not_an_object_is_refused_as_such(self) -> None:
        assert _refusal("list.json", b"[1, 2]").reason == "it is a JSON list, not a snapshot object"

    def test_a_document_that_is_not_a_snapshot_names_the_format_it_carries(self) -> None:
        reason = _refusal("export.json", _raw({"format": "something-else"})).reason
        assert "'format' is 'something-else'" in reason
        assert SNAPSHOT_FORMAT in reason

    def test_a_document_with_no_format_field_names_the_absence(self) -> None:
        assert "'format' is None" in _refusal("bare.json", b"{}").reason

    def test_a_snapshot_of_the_wrong_format_version_names_both_versions(self) -> None:
        reason = _refusal("future.json", _raw(_document(format_version=FORMAT_VERSION + 1))).reason
        assert f"'format_version' is {FORMAT_VERSION + 1}" in reason
        assert f"reads format_version {FORMAT_VERSION}" in reason

    def test_a_document_that_is_not_utf8_is_refused_before_the_parser(self) -> None:
        assert "not UTF-8" in _refusal("binary.json", b"\xff\xfe\x00\x01").reason

    def test_a_document_over_the_size_limit_is_refused_with_both_numbers(self) -> None:
        reason = _refusal("archive.json", b"x" * (MAX_SNAPSHOT_BYTES + 1)).reason
        assert str(MAX_SNAPSHOT_BYTES + 1) in reason
        assert str(MAX_SNAPSHOT_BYTES) in reason

    def test_one_bad_document_never_costs_the_good_ones(self) -> None:
        loaded = load_snapshots([("good.json", _raw(_document())), ("bad.json", b"{}")])
        assert [one.label for one in loaded.snapshots] == ["the vps (file 2026-07-30T09:12:00Z)"]
        assert [one.source for one in loaded.refusals] == ["bad.json"]


class TestALoadedColumnSaysItIsARecord(SimpleTestCase):
    """A reader who mistakes a record for a live reading gets the whole page wrong."""

    def _loaded(self, payload: dict[str, Any] | None = None) -> PeerSnapshot:
        return load_snapshots([("weeks-ago.json", _raw(payload or _document()))]).snapshots[0]

    def test_the_column_heading_says_it_came_from_a_file(self) -> None:
        assert "(file " in self._loaded().label

    def test_the_column_heading_carries_the_snapshots_own_capture_time(self) -> None:
        assert self._loaded().label == "the vps (file 2026-07-30T09:12:00Z)"

    def test_an_undated_snapshot_still_says_it_is_a_file(self) -> None:
        assert self._loaded(_document(captured_at="")).label == "the vps (file)"

    def test_a_snapshot_that_names_no_instance_falls_back_to_the_file_name(self) -> None:
        assert self._loaded(_document(instance={})).label.startswith("weeks-ago.json (file ")

    def test_the_capture_time_is_readable_on_its_own(self) -> None:
        assert self._loaded().captured_at == "2026-07-30T09:12:00Z"

    def test_the_origin_is_the_file_and_the_provenance_is_the_document(self) -> None:
        loaded = self._loaded()
        assert loaded.origin is SnapshotOrigin.FILE
        assert loaded.from_file
        assert loaded.provenance == "weeks-ago.json"

    def test_a_live_peer_is_still_live_and_states_its_url(self) -> None:
        peer = PeerSnapshot(label="box-b", url="http://127.0.0.1:8801/x", note="", payload=_document())
        assert peer.origin is SnapshotOrigin.LIVE
        assert not peer.from_file
        assert peer.provenance == "http://127.0.0.1:8801/x"


class TestTheNameASnapshotIsSavedUnder(SimpleTestCase):
    def test_it_carries_the_instance_label_and_the_capture_date(self) -> None:
        assert snapshot_filename(_document()) == "teatree-settings-the-vps-2026-07-30.json"

    def test_an_unlabelled_undated_payload_still_yields_a_usable_name(self) -> None:
        assert snapshot_filename({}) == "teatree-settings-instance-undated.json"

    def test_a_label_that_could_break_the_header_is_slugified_away(self) -> None:
        name = snapshot_filename(_document(instance={"label": 'a"; drop\n'}))
        assert '"' not in name
        assert "\n" not in name


class _CraftedComparison(SimpleTestCase):
    """Drives the real comparison over crafted instances, so the assertions are the page's."""

    def _view(self, local: PeerSnapshot, peers: tuple[PeerSnapshot, ...], loaded: tuple[PeerSnapshot, ...]) -> Any:
        with (
            patch("teatree.dash.settings_compare.local_snapshot", return_value=local),
            patch("teatree.dash.settings_compare.peer_snapshots", return_value=peers),
        ):
            return build_compare_view(loaded)


class TestARecordJoinsTheSameComparisonAsALivePeer(_CraftedComparison):
    def _box(self, label: str, stored: dict[str, dict[str, Any]]) -> PeerSnapshot:
        payload = _document(
            registry={"settings": {_UNSTORED_KEY: {"syncable": True, "sync_note": "", "category": ""}}},
            values={"settings": stored, "defaults": {}, "provenance": {}, "seed": {}, "seed_shipped": {}},
        )
        return PeerSnapshot(label=label, url="", note="", payload=payload)

    def test_a_record_alone_is_enough_to_compare_this_box_against(self) -> None:
        loaded = load_snapshots([("vps.json", _raw(self._box("ignored", {"": {_UNSTORED_KEY: 9}}).payload))])
        view = self._view(self._box("this instance", {"": {_UNSTORED_KEY: 4}}), (), loaded.snapshots)
        assert view.error == ""
        assert [(row.title, row.kind) for row in view.rows] == [(_UNSTORED_KEY, RowKind.VALUES)]

    def test_with_no_peer_and_no_file_the_page_names_both_ways_out(self) -> None:
        view = self._view(self._box("this instance", {}), (), ())
        assert "no reachable peer" in view.error
        assert "load a saved snapshot file" in view.error

    def test_a_record_sits_beside_the_live_peers_in_one_table(self) -> None:
        loaded = load_snapshots([("vps.json", _raw(self._box("the vps", {"": {_UNSTORED_KEY: 9}}).payload))])
        view = self._view(
            self._box("this instance", {"": {_UNSTORED_KEY: 4}}),
            (self._box("box-b", {"": {_UNSTORED_KEY: 4}}),),
            loaded.snapshots,
        )
        assert view.labels == ("this instance", "box-b", "the vps (file 2026-07-30T09:12:00Z)")
        for row in view.rows:
            assert [cell.label for cell in row.cells] == list(view.labels)


class TestARecordNeverDecidesWhetherTheLiveBoxesAgree(SimpleTestCase):
    """A schema skew between then and now is the record's age; between two live boxes it is code."""

    def _box(self, label: str, sha: str, origin: SnapshotOrigin = SnapshotOrigin.LIVE) -> PeerSnapshot:
        return PeerSnapshot(
            label=label,
            url="",
            note="",
            payload={"fingerprint": {"settings_schema_sha256": sha}},
            origin=origin,
        )

    def test_two_live_boxes_declaring_different_settings_still_block(self) -> None:
        report = build_compat_report([self._box("a", "ab"), self._box("b", "cd")])
        assert not report.comparable

    def test_a_record_declaring_different_settings_does_not_block(self) -> None:
        report = build_compat_report([self._box("a", "ab"), self._box("weeks ago", "cd", SnapshotOrigin.FILE)])
        assert report.comparable

    def test_that_disagreement_is_reported_as_info_never_dropped(self) -> None:
        report = build_compat_report([self._box("a", "ab"), self._box("weeks ago", "cd", SnapshotOrigin.FILE)])
        row = next(row for row in report.dated if row.signal.field == "settings_schema_sha256")
        assert row.signal.severity is Severity.BLOCKING
        assert row.severity is Severity.INFO
        assert row.readings == ("ab", "cd")

    def test_the_record_is_the_cell_marked_as_not_matching(self) -> None:
        report = build_compat_report([self._box("a", "ab"), self._box("weeks ago", "cd", SnapshotOrigin.FILE)])
        row = next(row for row in report.rows if row.signal.field == "settings_schema_sha256")
        assert [(cell.origin, cell.matches) for cell in row.cells] == [
            (SnapshotOrigin.LIVE, True),
            (SnapshotOrigin.FILE, False),
        ]

    def test_a_record_that_agrees_is_not_reported_as_dated(self) -> None:
        report = build_compat_report([self._box("a", "ab"), self._box("weeks ago", "ab", SnapshotOrigin.FILE)])
        assert report.dated == ()
        assert report.verdict == "comparable"


def _older(payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* as a teatree that predates one setting and never used one scope would emit it."""
    older = copy.deepcopy(payload)
    older["registry"]["settings"].pop(_UNSTORED_KEY, None)
    for bucket in older["values"]["settings"].values():
        bucket.pop(_UNSTORED_KEY, None)
    older["values"]["settings"].pop(_STORED_SCOPE, None)
    older["values"]["provenance"].pop(_STORED_SCOPE, None)
    for scope in older["values"]["provenance"].values():
        scope.pop(_UNSTORED_KEY, None)
    older["values"]["defaults"].pop(_UNSTORED_KEY, None)
    older["values"]["effective"].pop(_UNSTORED_KEY, None)
    older["fingerprint"]["settings_key_count"] = payload["fingerprint"]["settings_key_count"] - 1
    older["fingerprint"]["settings_schema_sha256"] = "0" * 64
    older["instance"]["label"] = "the same box, weeks ago"
    return older


class TestAnOlderRecordOfThisBoxIsStillComparable(TestCase):
    """The whole point of the feature: a REAL capture, hand-degraded, must not read as drift.

    A record's document legitimately lacks what today's code declares. If a missing key or a
    missing scope canonicalised to anything other than the silence it is, this box's 275
    declared settings would turn one degradation into hundreds of fabricated rows.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        # Class-level: build_snapshot derives every declared setting, the provenance of each scope
        # and a digest over every table, which is far too much to pay once per test method.
        super().setUpTestData()
        ConfigSetting.objects.set_value(_STORED_KEY, value=False, scope=_STORED_SCOPE)
        cls.payload = build_snapshot("this instance")
        cls.local = PeerSnapshot(label="this instance", url="", note="", payload=cls.payload)

    def _against(self, document: dict[str, Any]) -> Any:
        loaded = load_snapshots([("weeks-ago.json", _raw(document))])
        assert loaded.refusals == ()
        with (
            patch("teatree.dash.settings_compare.local_snapshot", return_value=self.local),
            patch("teatree.dash.settings_compare.peer_snapshots", return_value=()),
        ):
            return build_compare_view(loaded.snapshots)

    def test_a_record_of_this_box_as_it_is_differs_from_it_in_nothing(self) -> None:
        assert self._against(self.payload).total_rows == 0

    def test_a_key_the_records_code_never_declared_produces_no_row_at_all(self) -> None:
        view = self._against(_older(self.payload))
        assert [row for row in view.rows if row.title == _UNSTORED_KEY] == []

    def test_a_scope_the_record_never_used_costs_one_row_per_stored_value_not_per_key(self) -> None:
        view = self._against(_older(self.payload))
        scope_rows = [row for row in view.rows if row.scope == _STORED_SCOPE]
        assert [(row.title, row.kind) for row in scope_rows] == [(_STORED_KEY, RowKind.OVERRIDE)]
        assert len(self.payload["registry"]["settings"]) > len(scope_rows)

    def test_the_degradation_fabricates_no_value_difference(self) -> None:
        view = self._against(_older(self.payload))
        assert [row for row in view.rows if row.kind is RowKind.VALUES] == []

    def test_every_row_it_does_produce_is_backed_by_a_stored_value(self) -> None:
        view = self._against(_older(self.payload))
        assert all(any(cell.present for cell in row.cells) for row in view.rows)

    def test_a_key_this_box_stores_and_the_record_never_had_is_called_code_version(self) -> None:
        ConfigSetting.objects.set_value(_UNSTORED_KEY, 7, scope="")
        self.payload = build_snapshot("this instance")
        self.local = PeerSnapshot(label="this instance", url="", note="", payload=self.payload)
        view = self._against(_older(self.payload))
        rows = [row for row in view.rows if row.title == _UNSTORED_KEY]
        assert [(row.scope, row.kind) for row in rows] == [("", RowKind.CODE)]

    def test_the_older_schema_is_reported_as_the_records_age_not_as_a_blocker(self) -> None:
        compat = self._against(_older(self.payload)).compat
        assert compat.comparable
        assert {row.signal.field for row in compat.dated} == {"settings_schema_sha256", "settings_key_count"}
        assert all(row.severity is Severity.INFO for row in compat.dated)


class TestTheComparePageLoadsFiles(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.raw = _raw(build_snapshot("the vps"))

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("dash:settings_compare")

    def _upload(self, name: str = "vps.json") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, self.raw, content_type="application/json")

    def test_a_get_still_answers_the_live_comparison(self) -> None:
        response = self.client.get(self.url, **_LOOPBACK)
        assert response.status_code == 200
        assert b"no reachable peer" in response.content

    def test_an_uploaded_file_becomes_a_column(self) -> None:
        response = self.client.post(self.url, {"snapshot_files": self._upload()}, **_LOOPBACK)
        assert response.status_code == 200
        assert b"the vps (file " in response.content

    def test_pasted_json_becomes_a_column_too(self) -> None:
        response = self.client.post(self.url, {"snapshot_json": self.raw.decode()}, **_LOOPBACK)
        assert response.status_code == 200
        assert b"the vps (file " in response.content

    def test_several_files_all_become_columns(self) -> None:
        response = self.client.post(
            self.url,
            {"snapshot_files": [self._upload("a.json"), self._upload("b.json")]},
            **_LOOPBACK,
        )
        assert response.content.count(b"the vps (file ") >= 2

    def test_a_refused_file_says_which_one_and_why(self) -> None:
        bad = SimpleUploadedFile("bad.json", b"{}", content_type="application/json")
        response = self.client.post(self.url, {"snapshot_files": bad}, **_LOOPBACK)
        assert response.status_code == 400
        assert b"bad.json" in response.content
        assert b"was not loaded" in response.content

    def test_a_refused_file_beside_a_good_one_still_renders_the_comparison(self) -> None:
        bad = SimpleUploadedFile("bad.json", b"{}", content_type="application/json")
        response = self.client.post(self.url, {"snapshot_files": [self._upload(), bad]}, **_LOOPBACK)
        assert response.status_code == 200
        assert b"bad.json" in response.content
        assert b"the vps (file " in response.content

    def test_loading_writes_nothing(self) -> None:
        before = list(ConfigSetting.objects.values_list("scope", "key", "value"))
        self.client.post(self.url, {"snapshot_files": self._upload()}, **_LOOPBACK)
        assert list(ConfigSetting.objects.values_list("scope", "key", "value")) == before

    def test_the_load_route_is_refused_off_loopback(self) -> None:
        response = self.client.post(self.url, {"snapshot_files": self._upload()}, REMOTE_ADDR="10.0.0.9")
        assert response.status_code == 403


class TestSavingThisBoxsSnapshot(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("dash:settings_snapshot")

    def test_the_download_names_the_file_after_the_instance_and_the_capture_date(self) -> None:
        response = self.client.get(self.url, {"download": "1", "label": "the vps"}, **_LOOPBACK)
        assert response.status_code == 200
        assert response.headers["Content-Disposition"] == (
            f'attachment; filename="{snapshot_filename(response.json())}"'
        )
        assert response.headers["Content-Disposition"].startswith('attachment; filename="teatree-settings-the-vps-')

    def test_the_peer_fetch_is_untouched_by_the_download_flag(self) -> None:
        plain = self.client.get(self.url, {"label": "the vps"}, **_LOOPBACK)
        assert "Content-Disposition" not in plain.headers
        assert plain.json()["format"] == SNAPSHOT_FORMAT

    def test_the_downloaded_document_loads_straight_back_into_a_comparison(self) -> None:
        response = self.client.get(self.url, {"download": "1", "label": "the vps"}, **_LOOPBACK)
        loaded = load_snapshots([("downloaded.json", response.content)])
        assert loaded.refusals == ()
        assert loaded.snapshots[0].label.startswith("the vps (file ")

    def test_the_download_carries_no_raw_secret(self) -> None:
        response = self.client.get(self.url, {"download": "1"}, **_LOOPBACK)
        assert response.json()["includes_private"] is False

    def test_the_download_route_is_still_read_only(self) -> None:
        assert self.client.post(self.url, {"download": "1"}, **_LOOPBACK).status_code == 405

    def test_the_settings_page_offers_the_download(self) -> None:
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        assert f"{self.url}?download=1".encode() in response.content
