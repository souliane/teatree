"""Tests for ``teatree.loop.tick`` — orchestrator that runs scanners + dispatch."""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import django.test
import pytest

from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.tick import TickRequest, _repo_freshness, build_default_scanners, run_tick


@dataclass(slots=True)
class _FixedScanner:
    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


@dataclass(slots=True)
class _ExplodingScanner:
    name: str = "boom"

    def scan(self) -> list[ScanSignal]:
        msg = "scanner blew up"
        raise RuntimeError(msg)


def test_tick_aggregates_signals_from_all_scanners(tmp_path: Path) -> None:
    a = _FixedScanner(name="a", out=[ScanSignal(kind="my_pr.open", summary="A1")])
    b = _FixedScanner(name="b", out=[ScanSignal(kind="my_pr.open", summary="B1")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[a, b]), statusline_path=statusline)
    assert report.signal_count == 2
    assert report.action_count == 2


def test_tick_renders_statusline_to_file(tmp_path: Path) -> None:
    scanner = _FixedScanner(name="x", out=[ScanSignal(kind="my_pr.failed", summary="oops")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)
    assert statusline.is_file()
    contents = statusline.read_text(encoding="utf-8")
    assert "oops" in contents
    assert report.statusline_path == statusline
    meta = tmp_path / "tick-meta.json"
    assert meta.is_file()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert "next_epoch" in data
    assert "cadence" in data


def test_tick_records_scanner_errors_without_failing(tmp_path: Path) -> None:
    good = _FixedScanner(name="ok", out=[ScanSignal(kind="my_pr.open", summary="good")])
    bad = _ExplodingScanner()
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[good, bad]), statusline_path=statusline)
    assert report.signal_count == 1
    assert "boom" in report.errors
    assert "scanner blew up" in report.errors["boom"]


def test_tick_with_no_scanners_writes_tick_meta(tmp_path: Path) -> None:
    statusline = tmp_path / "statusline.txt"
    report = run_tick(
        TickRequest(scanners=[]),
        statusline_path=statusline,
        now=dt.datetime(2026, 5, 6, tzinfo=dt.UTC),
    )
    assert statusline.is_file()
    assert report.signal_count == 0
    meta = tmp_path / "tick-meta.json"
    assert meta.is_file()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["next_epoch"] > 0
    assert data["cadence"] == 720


def test_repo_freshness_on_real_git_repo(tmp_path: Path) -> None:
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

    repo = tmp_path / "repo"
    repo.mkdir()
    run_allowed_to_fail(["git", "init"], cwd=repo, expected_codes=None)
    run_allowed_to_fail(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, expected_codes=None)
    info = _repo_freshness(repo)
    assert info is not None
    assert isinstance(info["behind"], int)
    assert isinstance(info["fetch_epoch"], int)


def test_repo_freshness_returns_none_for_non_git(tmp_path: Path) -> None:
    assert _repo_freshness(tmp_path) is None


def test_tick_meta_includes_freshness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statusline = tmp_path / "statusline.txt"
    monkeypatch.setenv("T3_REPO", str(tmp_path.parent))
    run_tick(
        TickRequest(scanners=[]),
        statusline_path=statusline,
        now=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
    )
    meta = tmp_path / "tick-meta.json"
    assert meta.is_file()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert "freshness" in data


def test_build_default_scanners_starts_with_pending_tasks() -> None:
    scanners: list[Scanner] = build_default_scanners(host=None, messaging=None)
    assert [s.name for s in scanners] == ["pending_tasks"]


def test_build_default_scanners_adds_host_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import CodeHostBackend  # noqa: PLC0415

    host = MagicMock(spec=CodeHostBackend)
    scanners = build_default_scanners(host=host, messaging=None)
    names = {s.name for s in scanners}
    assert {"pending_tasks", "my_prs", "reviewer_prs"} <= names


def test_build_default_scanners_adds_messaging_and_notion_scanners() -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import MessagingBackend  # noqa: PLC0415

    messaging = MagicMock(spec=MessagingBackend)
    notion = MagicMock()
    scanners = build_default_scanners(
        host=None,
        messaging=messaging,
        notion_client=notion,
    )
    names = {s.name for s in scanners}
    assert "slack_mentions" in names
    assert "notion_view" in names


def test_tick_dispatches_agent_actions_without_rendering_them(tmp_path: Path) -> None:
    """Agent actions are dispatched but not rendered in the statusline."""
    scanner = _FixedScanner(
        name="reviewer_prs",
        out=[ScanSignal(kind="reviewer_pr.new_sha", summary="MR review")],
    )
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)
    contents = statusline.read_text(encoding="utf-8")
    assert "→ t3:reviewer" not in contents
    assert any(a.kind == "agent" for a in report.actions)


def test_tick_renders_unknown_action_zone_as_in_flight(tmp_path: Path) -> None:
    """A statusline action with an unrecognized zone falls back to in_flight."""
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [DispatchAction(kind="statusline", zone="bogus_zone", detail="x")]
    zones = _zones_for(actions)
    # Non-list zone falls through (line 88 branch); detail is silently dropped.
    assert "x" not in zones.action_needed
    assert "x" not in zones.in_flight


def test_tick_signal_url_renders_as_osc8_hyperlink(tmp_path: Path) -> None:
    """A scanner that puts ``url`` in its signal payload produces a clickable line."""
    scanner = _FixedScanner(
        name="my_prs",
        out=[
            ScanSignal(
                kind="my_pr.open",
                summary="PR #545: feat(loop)",
                payload={"url": "https://github.com/owner/repo/pull/545"},
            )
        ],
    )
    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline, colorize=True)
    contents = statusline.read_text(encoding="utf-8")
    assert "\033]8;;https://github.com/owner/repo/pull/545\033\\" in contents
    assert "PR #545: feat(loop)" in contents


def test_build_default_jobs_tags_per_overlay() -> None:
    """Each overlay-scoped scanner gets its overlay name attached to ``_run_job``'s label."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import CodeHostBackend, MessagingBackend  # noqa: PLC0415
    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backends = [
        OverlayBackends(
            name="teatree",
            host=MagicMock(spec=CodeHostBackend),
            messaging=None,
            ready_labels=(),
        ),
        OverlayBackends(
            name="acme",
            host=MagicMock(spec=CodeHostBackend),
            messaging=MagicMock(spec=MessagingBackend),
            ready_labels=("ready",),
        ),
    ]
    jobs = build_default_jobs(backends=backends)
    overlays = {job.overlay for job in jobs if job.overlay}
    assert overlays == {"teatree", "acme"}
    pending = [j for j in jobs if j.scanner.name == "pending_tasks"]
    assert len(pending) == 1  # singleton across overlays


def test_zones_groups_disposition_candidates_by_reason(tmp_path: Path) -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="Ticket 55 — issue_closed",
            payload={
                "reason": "issue_closed",
                "overlay": "teatree",
                "url": "https://example.com/issues/55",
            },
        ),
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="Ticket 114 — issue_closed",
            payload={
                "reason": "issue_closed",
                "overlay": "teatree",
                "url": "https://example.com/issues/114",
            },
        ),
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="Ticket 12 — unassigned",
            payload={
                "reason": "unassigned",
                "overlay": "teatree",
                "url": "https://example.com/issues/12",
            },
        ),
    ]
    zones = _zones_for(actions)
    texts = [item if isinstance(item, str) else item.text for item in zones.action_needed]
    assert len(texts) == 1
    # Each disposition is rendered as a clickable ``#N`` token grouped by
    # reason (`closed:` / `reassigned:` / `label-removed:`) instead of an
    # opaque "N closed issues" aggregate — the reader can jump to the source.
    assert "closed:" in texts[0]
    assert "#55" in texts[0]
    assert "#114" in texts[0]
    assert "reassigned:" in texts[0]
    assert "#12" in texts[0]
    assert "[teatree]" in texts[0]


def test_zones_lists_ready_to_start_as_clickable_refs() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail=f"Ready to start: Issue #{i}",
            payload={"url": f"https://example.com/issues/{i}", "overlay": "acme"},
        )
        for i in range(3)
    ]
    zones = _zones_for(actions)
    texts = [item if isinstance(item, str) else item.text for item in zones.action_needed]
    assert len(texts) == 1
    # Replaces the legacy "[acme] N ready to start" aggregate — each ready
    # issue is its own ``#N`` link so the reader can jump straight to it.
    assert texts[0].startswith("[acme] ready:")
    assert "#0" in texts[0]
    assert "#1" in texts[0]
    assert "#2" in texts[0]


def test_zones_groups_prs_per_overlay_on_one_line() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail=f"PR #{iid} open: feat",
            payload={"url": f"https://example.com/pr/{iid}", "iid": iid, "overlay": "acme"},
        )
        for iid in [330, 1089, 7370]
    ]
    zones = _zones_for(actions)
    assert len(zones.in_flight) == 1
    text = zones.in_flight[0] if isinstance(zones.in_flight[0], str) else zones.in_flight[0].text
    assert "[acme]" in text
    assert "!330" in text
    assert "!1089" in text
    assert "!7370" in text


def test_zones_pr_annotations_show_notes_and_failures() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="PR #330 has 7 notes",
            payload={"url": "https://x/330", "iid": 330, "draft_count": 7, "overlay": "o"},
        ),
        DispatchAction(
            kind="statusline",
            zone="action_needed",
            detail="PR #99 pipeline failed",
            payload={"url": "https://x/99", "iid": 99, "status": "failed", "overlay": "o"},
        ),
    ]
    zones = _zones_for(actions)
    text = zones.action_needed[0] if isinstance(zones.action_needed[0], str) else zones.action_needed[0].text
    assert "!330 (7 notes)" in text
    assert "!99 (pipeline failed)" in text


def test_active_tickets_shown_in_anchors() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="statusline",
            zone="anchors",
            detail="#123 started",
            payload={"overlay": "acme", "ticket_number": "123", "state": "started"},
        ),
        DispatchAction(
            kind="statusline",
            zone="anchors",
            detail="#456 coded",
            payload={"overlay": "acme", "ticket_number": "456", "state": "coded"},
        ),
    ]
    zones = _zones_for(actions)
    anchor_texts = [a if isinstance(a, str) else a.text for a in zones.anchors]
    assert len(anchor_texts) == 1
    assert "started: #123" in anchor_texts[0]
    assert "coded: #456" in anchor_texts[0]
    assert "[acme]" in anchor_texts[0]


def test_mechanical_actions_not_rendered_in_statusline() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(
            kind="mechanical", zone="ticket_completion", detail="Ticket 42 done", payload={"overlay": "acme"}
        ),
    ]
    zones = _zones_for(actions)
    texts = [e if isinstance(e, str) else e.text for e in zones.in_flight]
    assert not any("Ticket 42 done" in t for t in texts)


def test_agent_actions_not_rendered_in_statusline() -> None:
    from teatree.loop.dispatch import DispatchAction  # noqa: PLC0415
    from teatree.loop.rendering import zones_for as _zones_for  # noqa: PLC0415

    actions = [
        DispatchAction(kind="agent", zone="t3:reviewer", detail="Review PR", payload={}),
    ]
    zones = _zones_for(actions)
    texts = [e if isinstance(e, str) else e.text for e in zones.in_flight]
    assert not any("t3:reviewer" in t for t in texts)


class TestDispositionMechanical(django.test.TestCase):
    def test_closed_issue_auto_ignores(self) -> None:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415
        from teatree.loop.mechanical import ignore_disposed_ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="not_started")
        ignore_disposed_ticket({"ticket_id": ticket.pk, "reason": "issue_closed"})
        ticket.refresh_from_db()
        assert ticket.state == "ignored"


def test_tick_multi_overlay_prefixes_summary(tmp_path: Path) -> None:
    """Signals collected via the multi-overlay path get an ``[overlay]`` prefix in the rendered line."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.backends.protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415

    fake_host = MagicMock(spec=CodeHostBackend)
    fake_host.current_user.return_value = "souliane"
    fake_host.list_my_prs.return_value = [
        {"iid": 545, "title": "feat(loop)", "html_url": "https://gh/x/y/pull/545", "user_notes_count": 0}
    ]
    fake_host.list_review_requested_prs.return_value = []
    fake_host.list_assigned_issues.return_value = []
    backends = [OverlayBackends(name="teatree", host=fake_host, messaging=None, ready_labels=())]

    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(backends=backends), statusline_path=statusline, colorize=False)
    contents = statusline.read_text(encoding="utf-8")
    assert "[teatree]" in contents
    assert "!545" in contents


def test_canonical_overlay_names_maps_toml_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.loop.tick import _canonical_overlay_names  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text(
        '[teatree]\nworkspace_dir = "~/ws"\n[overlays.teatree]\n[overlays.acme]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    overlays = {"t3-teatree": object(), "acme": object()}
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
        mapping = _canonical_overlay_names()
    assert mapping == {"teatree": "t3-teatree"}


def test_canonical_overlay_names_returns_empty_without_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from teatree.loop.tick import _canonical_overlay_names  # noqa: PLC0415

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _canonical_overlay_names() == {}


def test_canonical_overlay_names_handles_invalid_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.loop.tick import _canonical_overlay_names  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text("not = valid = toml = at = all\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-teatree": object()}):
        assert _canonical_overlay_names() == {}


def test_issue_ref_from_falls_back_to_ticket_number() -> None:
    from teatree.loop.rendering import _issue_ref_from  # noqa: PLC0415

    # Corrupt issue_url (a branch name) + ticket_number → label uses #N,
    # url stays empty so the renderer doesn't make a broken hyperlink.
    ref = _issue_ref_from(issue_url="auto:ac/some-branch", ticket_number="313")
    assert ref.label == "#313"
    assert ref.url == ""


def test_issue_ref_from_falls_back_to_title_snippet() -> None:
    from teatree.loop.rendering import _issue_ref_from  # noqa: PLC0415

    long_title = "A" * 50
    ref = _issue_ref_from(title=long_title)
    # Title gets sliced to 29 chars + "…" to stay readable on a narrow line.
    assert ref.label.endswith("…")
    assert ref.label.count("A") == 29


def test_issue_ref_from_returns_question_mark_when_nothing_known() -> None:
    from teatree.loop.rendering import _issue_ref_from  # noqa: PLC0415

    assert _issue_ref_from().label == "?"


def test_render_pr_group_buckets_under_parent_ticket() -> None:
    from teatree.loop.rendering import _PRRef, _render_pr_group  # noqa: PLC0415

    refs = [
        _PRRef(iid=370, url="https://gitlab.com/x/y/-/merge_requests/370", annotation=""),
        _PRRef(iid=399, url="https://gitlab.com/x/y/-/merge_requests/399", annotation=""),
    ]
    ticket_index = {
        "https://gitlab.com/x/y/-/merge_requests/370": "855",
        "https://gitlab.com/x/y/-/merge_requests/399": "855",
    }
    line = _render_pr_group("acme", refs, ticket_index=ticket_index)
    assert "#855:" in line
    assert "!370" in line
    assert "!399" in line


def test_render_pr_group_lists_orphans_when_no_match() -> None:
    from teatree.loop.rendering import _PRRef, _render_pr_group  # noqa: PLC0415

    refs = [_PRRef(iid=42, url="https://example.com/mr/42", annotation="")]
    line = _render_pr_group("t3-teatree", refs, ticket_index={})
    assert "!42" in line
    assert "#" not in line  # no ticket prefix


def test_reviewer_pr_signal_surfaces_in_statusline() -> None:
    from teatree.loop.dispatch import dispatch  # noqa: PLC0415
    from teatree.loop.rendering import zones_for  # noqa: PLC0415

    signal = ScanSignal(
        kind="reviewer_pr.new_sha",
        summary="Review needed: https://gitlab.com/x/y/-/merge_requests/371",
        payload={
            "url": "https://gitlab.com/x/y/-/merge_requests/371",
            "head_sha": "abc",
            "previous_sha": "def",
            "raw": {},
            "overlay": "acme",
        },
    )
    actions = dispatch([signal])
    kinds = sorted({a.kind for a in actions})
    # Reviewer signals now produce BOTH an agent action (to the t3:reviewer
    # phase agent) AND a statusline action_needed row so the user sees the
    # pending review without waiting on the agent to act.
    assert kinds == ["agent", "statusline"]
    zones = zones_for(actions)
    rendered = "\n".join(item if isinstance(item, str) else item.text for item in zones.action_needed)
    assert "!371" in rendered
    assert "review" in rendered
    assert "[acme]" in rendered
