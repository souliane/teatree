"""Tests for the tabular per-tick dashboard (#1005).

The dashboard reads from a ``tick-actions.jsonl`` sidecar and renders a
markdown table grouped by overlay. These tests cover:

* recording: identity-dedup, cross-overlay bleed gate, rotation
* rendering: linking, grouping, deduplication, self-DM marker
* sending: idempotency key shape, content-hash bucketing
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from teatree.loop.dashboard import (
    DashboardFormat,
    TickAction,
    content_hash,
    default_actions_path,
    idempotency_key_for,
    record_actions,
    render_dashboard,
    send_dashboard,
)
from teatree.loop.dispatch import DispatchAction


def _action(
    *,
    kind: str = "statusline",
    zone: str = "action_needed",
    detail: str = "Review needed: MR-42",
    payload: dict[str, object] | None = None,
) -> DispatchAction:
    return DispatchAction(kind=kind, zone=zone, detail=detail, payload=payload or {})


# ── recording ────────────────────────────────────────────────────────


class TestRecordActions:
    def test_writes_one_jsonl_line_per_action(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                detail="Review needed: MR-42",
                payload={
                    "iid": 42,
                    "url": "https://gitlab.example/group/repo/-/merge_requests/42",
                    "overlay": "acme",
                    "title": "Fix the thing",
                },
            ),
            _action(
                zone="anchors",
                detail="ticket #58",
                payload={
                    "overlay": "acme",
                    "state": "scoped",
                    "ticket_number": "58",
                    "issue_url": "https://gitlab.example/group/repo/-/issues/58",
                },
            ),
        ]
        now = dt.datetime(2026, 5, 19, 8, 30, 0, tzinfo=dt.UTC)
        written = record_actions(actions, now=now, path=path)

        assert written == 2
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        row_0 = json.loads(lines[0])
        assert row_0["overlay"] == "acme"
        assert row_0["ref"] == "!42"
        assert row_0["url"].endswith("/merge_requests/42")
        assert row_0["label"] == "Fix the thing"
        assert row_0["ts"].startswith("2026-05-19T08:30:00")

    def test_skips_unrecognised_action_kinds(self, tmp_path: Path) -> None:
        # ``kind`` outside the known set never makes it into the sidecar.
        path = tmp_path / "tick-actions.jsonl"
        actions = [_action(kind="surprise", payload={"overlay": "acme"})]
        assert record_actions(actions, now=dt.datetime.now(dt.UTC), path=path) == 0
        assert not path.is_file()

    def test_identity_dedup_filters_reassignment_between_aliases(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                detail="reassigned",
                payload={
                    "overlay": "acme",
                    "reason": "unassigned",
                    "old_owner": "alice-gh",
                    "new_owners": ["souliane"],
                    "url": "https://gitlab.example/group/repo/-/issues/77",
                    "iid": 77,
                },
            ),
            _action(
                detail="reassigned to outsider",
                payload={
                    "overlay": "acme",
                    "reason": "unassigned",
                    "old_owner": "alice-gh",
                    "new_owners": ["someone-else"],
                    "url": "https://gitlab.example/group/repo/-/issues/78",
                    "iid": 78,
                },
            ),
        ]
        written = record_actions(
            actions,
            now=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
            path=path,
            identities=["alice-gh", "souliane", "adrien.cossa"],
        )

        # Only the cross-identity-set reassignment survives.
        assert written == 1
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["ref"] == "#78"

    def test_cross_overlay_bleed_gate_drops_foreign_url(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "acme",
                    "iid": 99,
                    "url": "https://gitlab.example/foreign/repo/-/merge_requests/99",
                    "title": "Foreign",
                },
            ),
            _action(
                payload={
                    "overlay": "acme",
                    "iid": 100,
                    "url": "https://gitlab.example/acme/acme-product/-/merge_requests/100",
                    "title": "Owned",
                },
            ),
        ]
        written = record_actions(
            actions,
            now=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
            path=path,
            overlay_repos={"acme": frozenset({"acme-product"})},
        )
        assert written == 1
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["ref"] == "!100"

    def test_rotation_keeps_recent_half_when_limit_exceeded(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        # Seed the file at the limit.
        path.write_text(
            "\n".join(json.dumps({"ts": str(i), "ref": f"#{i}"}) for i in range(1000)) + "\n",
            encoding="utf-8",
        )
        actions = [_action(payload={"overlay": "x", "iid": 1234, "url": "https://example/x/-/issues/1234"})]
        record_actions(actions, now=dt.datetime(2026, 5, 19, tzinfo=dt.UTC), path=path)

        lines = path.read_text(encoding="utf-8").splitlines()
        # Half (500) survives the rotation, plus the new row.
        assert len(lines) == 501
        # Oldest survivor is the row at index 500 in the seed.
        first = json.loads(lines[0])
        assert first["ref"] == "#500"

    def test_no_rotation_below_limit(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        path.write_text(
            "\n".join(json.dumps({"ts": str(i), "ref": f"#{i}"}) for i in range(10)) + "\n",
            encoding="utf-8",
        )
        actions = [_action(payload={"overlay": "x", "iid": 5, "url": "https://example/x/-/issues/5"})]
        record_actions(actions, now=dt.datetime(2026, 5, 19, tzinfo=dt.UTC), path=path)
        assert len(path.read_text(encoding="utf-8").splitlines()) == 11

    def test_empty_actions_does_not_create_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        assert record_actions([], now=dt.datetime.now(dt.UTC), path=path) == 0
        assert not path.is_file()

    def test_url_only_action_derives_ref_from_url_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "t3-teatree",
                    "url": "https://github.com/souliane/teatree/issues/123",
                },
            ),
        ]
        record_actions(actions, now=dt.datetime.now(dt.UTC), path=path)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["ref"] == "#123"


# ── rendering ────────────────────────────────────────────────────────


class TestRenderDashboard:
    def _seed(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )

    def test_empty_sidecar_renders_placeholder(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        out = render_dashboard(source_path=path)
        assert "_No tick actions recorded yet._" in out

    def test_markdown_table_grouped_by_overlay(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "ts": "2026-05-19T09:00:00+00:00",
                    "scanner": "MyPrsScanner",
                    "overlay": "acme",
                    "action_kind": "statusline",
                    "ref": "!42",
                    "label": "Fix offer",
                    "url": "https://gitlab.example/acme/-/merge_requests/42",
                    "before_state": "draft",
                    "after_state": "review",
                },
                {
                    "ts": "2026-05-19T09:00:00+00:00",
                    "scanner": "AssignedIssuesScanner",
                    "overlay": "t3-teatree",
                    "action_kind": "statusline",
                    "ref": "#58",
                    "label": "Add dashboard",
                    "url": "https://github.com/souliane/teatree/issues/58",
                    "before_state": "",
                    "after_state": "ready",
                },
            ],
        )
        out = render_dashboard(source_path=path, fmt=DashboardFormat.MARKDOWN)

        assert "## [acme]" in out
        assert "## [t3-teatree]" in out
        assert "| Ref | Title | State | Last action | URL |" in out
        # Markdown links
        assert "[!42](https://gitlab.example/acme/-/merge_requests/42)" in out
        assert "[#58](https://github.com/souliane/teatree/issues/58)" in out
        # state transition cell
        assert "draft → review" in out
        # last-action cell joins scanner + kind
        assert "MyPrsScanner · statusline" in out

    def test_slack_format_emits_mrkdwn_pipe_links(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "scanner": "MyPrsScanner",
                    "overlay": "acme",
                    "action_kind": "statusline",
                    "ref": "!42",
                    "label": "Title with | pipe",
                    "url": "https://gitlab.example/acme/-/merge_requests/42",
                },
            ],
        )
        out = render_dashboard(source_path=path, fmt=DashboardFormat.SLACK)
        assert "<https://gitlab.example/acme/-/merge_requests/42|!42>" in out

    def test_two_distinct_rows_same_overlay_share_one_bucket(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "overlay": "acme",
                    "ref": "!42",
                    "label": "first",
                    "url": "https://example/-/merge_requests/42",
                    "action_kind": "statusline",
                    "scanner": "",
                },
                {
                    "overlay": "acme",
                    "ref": "!43",
                    "label": "second",
                    "url": "https://example/-/merge_requests/43",
                    "action_kind": "statusline",
                    "scanner": "",
                },
            ],
        )
        out = render_dashboard(source_path=path)
        # Both rows appear under a single overlay header.
        assert out.count("## [acme]") == 1
        assert "first" in out
        assert "second" in out

    def test_dedupes_same_overlay_ref_url_triple(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "overlay": "acme",
                    "ref": "!42",
                    "label": "First sighting",
                    "url": "https://example/x/-/merge_requests/42",
                    "action_kind": "statusline",
                    "scanner": "MyPrsScanner",
                },
                {
                    "overlay": "acme",
                    "ref": "!42",
                    "label": "Second sighting same MR",
                    "url": "https://example/x/-/merge_requests/42",
                    "action_kind": "statusline",
                    "scanner": "ReviewerPrsScanner",
                },
            ],
        )
        out = render_dashboard(source_path=path)
        assert out.count("| [!42]") == 1
        assert "First sighting" in out  # first wins
        assert "Second sighting" not in out

    def test_self_dm_marker_tags_slack_dm_row(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "overlay": "acme",
                    "ref": "dm",
                    "label": "Dashboard delivered",
                    "url": "",
                    "action_kind": "slack_dm",
                    "scanner": "notify_user",
                },
            ],
        )
        out = render_dashboard(source_path=path, self_dm_marker=True)
        assert "(this DM)" in out

    def test_unscoped_rows_get_their_own_section(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "overlay": "",
                    "ref": "#1",
                    "label": "anything",
                    "url": "",
                    "action_kind": "statusline",
                    "scanner": "",
                },
            ],
        )
        out = render_dashboard(source_path=path)
        assert "## [(unscoped)]" in out

    def test_no_url_falls_back_to_dash(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        self._seed(
            path,
            [
                {
                    "overlay": "x",
                    "ref": "",
                    "label": "no link",
                    "url": "",
                    "action_kind": "statusline",
                    "scanner": "",
                },
            ],
        )
        out = render_dashboard(source_path=path)
        # Ref column "—" and URL column "—".
        assert "| — | no link |" in out
        assert " — |\n" in out

    def test_malformed_jsonl_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        good = json.dumps({"overlay": "x", "ref": "#1", "label": "ok"})
        path.write_text(f"not-json\n{good}\n", encoding="utf-8")
        out = render_dashboard(source_path=path)
        assert "ok" in out

    def test_missing_file_renders_empty(self, tmp_path: Path) -> None:
        out = render_dashboard(source_path=tmp_path / "does-not-exist.jsonl")
        assert "_No tick actions recorded yet._" in out


# ── idempotency / send ──────────────────────────────────────────────


class TestIdempotency:
    def test_same_content_same_bucket_yields_same_key(self) -> None:
        ts1 = dt.datetime(2026, 5, 19, 9, 0, 0, tzinfo=dt.UTC)
        ts2 = dt.datetime(2026, 5, 19, 9, 4, 30, tzinfo=dt.UTC)  # same 5-min bucket
        rendered = "# dashboard\n| a | b |\n"
        assert idempotency_key_for(rendered, tick_ts=ts1) == idempotency_key_for(rendered, tick_ts=ts2)

    def test_different_content_changes_key(self) -> None:
        ts = dt.datetime(2026, 5, 19, 9, 0, 0, tzinfo=dt.UTC)
        a = idempotency_key_for("one", tick_ts=ts)
        b = idempotency_key_for("two", tick_ts=ts)
        assert a != b

    def test_different_bucket_changes_key(self) -> None:
        ts1 = dt.datetime(2026, 5, 19, 9, 0, 0, tzinfo=dt.UTC)
        ts2 = dt.datetime(2026, 5, 19, 9, 6, 0, tzinfo=dt.UTC)  # next bucket
        assert idempotency_key_for("same", tick_ts=ts1) != idempotency_key_for("same", tick_ts=ts2)

    def test_content_hash_is_stable_short_hex(self) -> None:
        h = content_hash("x")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.seen_keys: dict[str, bool] = {}

    def __call__(self, text: str, *, kind: str, idempotency_key: str) -> bool:
        # Real notify_user is idempotent on the key; mirror that here.
        if idempotency_key in self.seen_keys:
            return self.seen_keys[idempotency_key]
        self.calls.append({"text": text, "kind": kind, "idempotency_key": idempotency_key})
        self.seen_keys[idempotency_key] = True
        return True


class TestSendDashboard:
    def test_calls_notify_with_idempotency_key(self) -> None:
        notifier = _RecordingNotifier()
        ts = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
        sent = send_dashboard("hello", tick_ts=ts, notify_user_fn=notifier)

        assert sent is True
        assert len(notifier.calls) == 1
        call = notifier.calls[0]
        assert call["text"] == "hello"
        assert call["kind"] == "info"
        assert call["idempotency_key"].startswith("dashboard-")

    def test_second_send_within_bucket_is_a_noop(self) -> None:
        notifier = _RecordingNotifier()
        ts = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
        send_dashboard("hello", tick_ts=ts, notify_user_fn=notifier)
        send_dashboard("hello", tick_ts=ts, notify_user_fn=notifier)
        # Only one actual call recorded — the second was deduped by the notifier
        # using the idempotency key.
        assert len(notifier.calls) == 1

    def test_notifier_returning_false_propagates(self) -> None:
        def notifier(_text: str, *, kind: str, idempotency_key: str) -> bool:
            _ = kind, idempotency_key
            return False

        sent = send_dashboard(
            "hello",
            tick_ts=dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC),
            notify_user_fn=notifier,
        )
        assert sent is False


class TestIdentityDedupEdgeCases:
    """Cover the defensive guards in ``_is_identity_self_handoff``."""

    def test_no_identities_disables_dedup(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "x",
                    "reason": "unassigned",
                    "old_owner": "a",
                    "new_owners": ["b"],
                    "iid": 1,
                    "url": "https://example/-/issues/1",
                },
            ),
        ]
        # Without identities passed, even an intra-alias move is kept.
        written = record_actions(actions, now=dt.datetime.now(dt.UTC), path=path, identities=[])
        assert written == 1

    def test_unassigned_with_bad_payload_shape_is_kept(self, tmp_path: Path) -> None:
        """`new_owners` not a list → guard returns False, row is kept."""
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "x",
                    "reason": "unassigned",
                    "old_owner": "a",
                    "new_owners": "not-a-list",
                    "iid": 2,
                    "url": "https://example/-/issues/2",
                },
            ),
        ]
        assert record_actions(actions, now=dt.datetime.now(dt.UTC), path=path, identities=["a"]) == 1

    def test_unassigned_empty_new_owners_is_kept(self, tmp_path: Path) -> None:
        """`new_owners=[]` is not a self-handoff (no destinations to compare)."""
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "x",
                    "reason": "unassigned",
                    "old_owner": "a",
                    "new_owners": [],
                    "iid": 3,
                    "url": "https://example/-/issues/3",
                },
            ),
        ]
        assert record_actions(actions, now=dt.datetime.now(dt.UTC), path=path, identities=["a"]) == 1

    def test_unassigned_old_owner_outside_identities_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "x",
                    "reason": "unassigned",
                    "old_owner": "outsider",
                    "new_owners": ["adrien"],
                    "iid": 4,
                    "url": "https://example/-/issues/4",
                },
            ),
        ]
        assert record_actions(actions, now=dt.datetime.now(dt.UTC), path=path, identities=["adrien"]) == 1


class TestCrossOverlayBleedEdgeCases:
    def test_overlay_not_in_repos_map_is_permissive(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "untracked-overlay",
                    "iid": 1,
                    "url": "https://example/-/merge_requests/1",
                },
            ),
        ]
        # Empty repos_map for "untracked-overlay" → keep the row.
        written = record_actions(
            actions,
            now=dt.datetime.now(dt.UTC),
            path=path,
            overlay_repos={"some-other-overlay": frozenset({"repo"})},
        )
        assert written == 1

    def test_empty_repos_set_for_overlay_is_permissive(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(
                payload={
                    "overlay": "ov",
                    "iid": 2,
                    "url": "https://example/-/merge_requests/2",
                },
            ),
        ]
        # `frozenset()` is empty → guard returns True early, row is kept.
        written = record_actions(
            actions,
            now=dt.datetime.now(dt.UTC),
            path=path,
            overlay_repos={"ov": frozenset()},
        )
        assert written == 1

    def test_overlay_set_but_action_has_no_url_is_permissive(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        actions = [
            _action(payload={"overlay": "ov", "iid": 99}),
        ]
        written = record_actions(
            actions,
            now=dt.datetime.now(dt.UTC),
            path=path,
            overlay_repos={"ov": frozenset({"repo"})},
        )
        assert written == 1


class TestDefaultActionsPath:
    def test_default_path_lives_under_teatree_data_dir(self) -> None:
        path = default_actions_path()
        assert path.name == "tick-actions.jsonl"
        assert "teatree" in str(path)


class TestNonDictPayloadGuards:
    """``DispatchAction.payload`` is typed ``dict[str, Any]`` but be paranoid."""

    def test_record_skips_action_with_non_string_overlay_field(self, tmp_path: Path) -> None:
        """A row whose `overlay` is not a string degrades to no-overlay (kept)."""
        path = tmp_path / "tick-actions.jsonl"
        actions = [_action(payload={"overlay": 123, "iid": 1, "url": "https://x/-/merge_requests/1"})]
        written = record_actions(actions, now=dt.datetime.now(dt.UTC), path=path)
        assert written == 1
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["overlay"] == ""

    def test_load_actions_skips_non_dict_jsonl_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        # Valid JSON but not a dict — must be silently dropped.
        path.write_text(
            json.dumps(["not", "a", "dict"]) + "\n" + json.dumps({"overlay": "x", "ref": "#5", "label": "ok"}) + "\n",
            encoding="utf-8",
        )
        out = render_dashboard(source_path=path)
        assert "ok" in out
        assert "not" not in out

    def test_load_actions_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "tick-actions.jsonl"
        path.write_text(
            "\n\n" + json.dumps({"overlay": "x", "ref": "#1", "label": "kept"}) + "\n\n",
            encoding="utf-8",
        )
        out = render_dashboard(source_path=path)
        assert "kept" in out


class TestTickActionDataclass:
    def test_round_trip_serialisation(self) -> None:
        row = TickAction(
            ts="2026-05-19T09:00:00+00:00",
            scanner="MyPrsScanner",
            overlay="acme",
            action_kind="statusline",
            ref="!42",
            label="Fix",
            url="https://example/-/merge_requests/42",
            before_state="draft",
            after_state="review",
        )
        decoded = TickAction.from_dict(row.to_dict())
        assert decoded == row

    def test_from_dict_with_missing_fields_uses_empty_defaults(self) -> None:
        row = TickAction.from_dict({})
        assert row.overlay == ""
        assert row.ref == ""

    @pytest.mark.parametrize(
        ("payload", "expected_ref"),
        [
            ({"iid": 5, "url": "https://x/-/merge_requests/5"}, "!5"),
            ({"iid": 5, "url": "https://x/-/issues/5"}, "#5"),
            ({"url": "https://x/-/merge_requests/77"}, "!77"),
            ({"url": "https://x/-/issues/77"}, "#77"),
            ({"ticket_number": "9"}, "#9"),
            ({}, ""),
        ],
    )
    def test_ref_derivation_table(self, payload: dict[str, object], expected_ref: str, tmp_path: Path) -> None:
        path = tmp_path / "actions.jsonl"
        record_actions(
            [_action(payload=payload)],
            now=dt.datetime.now(dt.UTC),
            path=path,
        )
        if not expected_ref and not payload:
            row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            assert row["ref"] == ""
            return
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert row["ref"] == expected_ref
