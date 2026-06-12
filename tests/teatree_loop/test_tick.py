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


def test_tick_meta_cadence_falls_back_to_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #1036: tick-meta.json `cadence` (drives the statusline next-tick
    # countdown) must honor ~/.teatree.toml loop_cadence_seconds when
    # T3_LOOP_CADENCE is unset, never diverging from the slot cadence.
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 300\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    statusline = tmp_path / "statusline.txt"
    started = dt.datetime(2026, 5, 6, tzinfo=dt.UTC)
    run_tick(TickRequest(scanners=[]), statusline_path=statusline, now=started)
    meta = tmp_path / "tick-meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["cadence"] == 300
    assert data["next_epoch"] == int(started.timestamp()) + 300


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


def test_build_default_scanners_starts_with_pending_tasks_incoming_events_outbound_audit() -> None:
    scanners: list[Scanner] = build_default_scanners(host=None, messaging=None)
    # #1191 added scanning_news as a fourth always-on global scanner; the
    # previous three must remain at the head of the list so existing
    # ordering contracts (FIFO write to the statusline buffer) hold.
    assert [s.name for s in scanners[:3]] == ["pending_tasks", "incoming_events", "outbound_audit"]
    assert "scanning_news" in {s.name for s in scanners}


def test_build_default_scanners_adds_host_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415

    host = MagicMock(spec=CodeHostBackend)
    scanners = build_default_scanners(host=host, messaging=None)
    names = {s.name for s in scanners}
    assert {"pending_tasks", "my_prs", "reviewer_prs"} <= names


def test_build_default_scanners_adds_messaging_and_notion_scanners() -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_protocols import MessagingBackend  # noqa: PLC0415

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

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backends = [
        OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
        ),
        OverlayBackends(
            name="acme",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=MagicMock(spec=MessagingBackend),
            ready_labels=("ready",),
        ),
    ]
    jobs = build_default_jobs(backends=backends)
    overlays = {job.overlay for job in jobs if job.overlay}
    assert overlays == {"teatree", "acme"}
    pending = [j for j in jobs if j.scanner.name == "pending_tasks"]
    assert len(pending) == 1  # singleton across overlays
    # The stale-tickets scanner (#563) is wired once per overlay.
    stale = {j.overlay for j in jobs if j.scanner.name == "stale_tickets"}
    assert stale == {"teatree", "acme"}


def test_build_default_jobs_propagates_user_identity_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``user_identity_aliases`` from ~/.teatree.toml lands on TicketDispositionScanner.

    Wiring proof for #975 — the loop reads the global setting and hands
    it to every overlay's disposition scanner so the reassign-suppression
    branch fires in production.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    config_path.write_text(
        '[teatree]\nuser_identity_aliases = ["adrien.work", "souliane", "acme.work"]\n',
        encoding="utf-8",
    )
    import teatree.config as _config  # noqa: PLC0415

    monkeypatch.setattr("teatree.loop.scanner_factory_config.load_config", lambda: _config.load_config(config_path))
    monkeypatch.setattr("teatree.loop.scanner_factory_config.discover_overlays", list)
    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", list)

    backends = [
        OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
        ),
    ]
    jobs = build_default_jobs(backends=backends)
    disp = next(j for j in jobs if j.scanner.name == "ticket_dispositions")
    assert disp.scanner.user_identity_aliases == ("adrien.work", "souliane", "acme.work")


def test_user_identity_aliases_falls_back_to_empty_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken config read never crashes the tick — defaults to empty aliases."""
    from teatree.loop.tick import _user_identity_aliases_for_overlay  # noqa: PLC0415

    def _boom() -> object:
        msg = "toml parse failure"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.loop.scanner_factory_config.load_config", _boom)
    assert _user_identity_aliases_for_overlay("acme") == ()


def test_user_identity_aliases_no_override_inherits_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlay registered without a per-overlay override sees the global setting."""
    from teatree.config import OverlayEntry  # noqa: PLC0415
    from teatree.loop.tick import _user_identity_aliases_for_overlay  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    config_path.write_text('[teatree]\nuser_identity_aliases = ["a", "b"]\n', encoding="utf-8")
    import teatree.config as _config  # noqa: PLC0415

    monkeypatch.setattr("teatree.loop.scanner_factory_config.load_config", lambda: _config.load_config(config_path))
    monkeypatch.setattr(
        "teatree.loop.scanner_factory_config.discover_overlays",
        lambda: [OverlayEntry(name="acme", overlay_class="x.y:Z", overrides={})],
    )
    assert _user_identity_aliases_for_overlay("acme") == ("a", "b")


def test_build_default_jobs_per_overlay_alias_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-overlay override beats the global ``user_identity_aliases`` for that overlay.

    The setting is registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` (#975),
    so a tracker-scoped overlay can carry tracker-specific handles
    without flipping the global default.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.config import OverlayEntry  # noqa: PLC0415
    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    config_path.write_text(
        '[teatree]\nuser_identity_aliases = ["global-only"]\n',
        encoding="utf-8",
    )
    import teatree.config as _config  # noqa: PLC0415

    monkeypatch.setattr("teatree.loop.scanner_factory_config.load_config", lambda: _config.load_config(config_path))
    monkeypatch.setattr(
        "teatree.loop.scanner_factory_config.discover_overlays",
        lambda: [
            OverlayEntry(
                name="scoped",
                overlay_class="x.y:Z",
                overrides={"user_identity_aliases": ["adrien.work", "souliane"]},
            ),
        ],
    )
    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", list)

    backends = [
        OverlayBackends(
            name="scoped",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
        ),
    ]
    jobs = build_default_jobs(backends=backends)
    disp = next(j for j in jobs if j.scanner.name == "ticket_dispositions")
    assert disp.scanner.user_identity_aliases == ("adrien.work", "souliane")


def test_identity_alias_groups_reads_overlay_config_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_identity_alias_groups_for_overlay`` prefers the live ``OverlayConfig``.

    A user who configures groups via an overlay's settings module (or
    overlay-class default) sees them honoured without going through the
    TOML override path — the source of truth is the live config object.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415
    from teatree.loop.tick import _identity_alias_groups_for_overlay  # noqa: PLC0415

    overlay = MagicMock(spec=OverlayBase)
    overlay.config = MagicMock()
    overlay.config.identity_aliases = [
        ["acme-gh", "souliane", "acme.work"],
        ["alice", "alice.work"],
    ]
    backend = OverlayBackends(
        name="acme",
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
    )
    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", list)
    groups = _identity_alias_groups_for_overlay("acme", backend)
    assert groups == (
        ("acme-gh", "souliane", "acme.work"),
        ("alice", "alice.work"),
    )


def test_identity_aliases_for_request_unions_across_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_identity_aliases_for_request`` unions every overlay's alias groups.

    The renderer suppresses a self-reassignment only when both ends fall
    inside one group; collecting every overlay's groups means the operator's
    own handles suppress regardless of which overlay surfaced the reassign.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415
    from teatree.loop.tick import TickRequest, _identity_aliases_for_request  # noqa: PLC0415

    def _backend(name: str, aliases: list[list[str]]) -> OverlayBackends:
        overlay = MagicMock(spec=OverlayBase)
        overlay.config = MagicMock()
        overlay.config.identity_aliases = aliases
        return OverlayBackends(
            name=name,
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
            overlay=overlay,
        )

    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", list)
    request = TickRequest(
        backends=[
            _backend("teatree", [["souliane", "op-alt", "op.work"]]),
            _backend("acme", [["alice", "alice.work"]]),
        ],
    )
    assert _identity_aliases_for_request(request) == (
        ("souliane", "op-alt", "op.work"),
        ("alice", "alice.work"),
    )


def test_identity_aliases_for_request_falls_back_to_operator_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit ``identity_aliases`` → ``backend.identities`` is the self-group (#1113).

    The deployment that surfaced the noise configures only the flat
    ``user_identity_aliases`` (→ ``backend.identities``), never the grouped
    ``identity_aliases``. The render path must apply the same self-group
    fallback the scanner path already had, or every intra-self reassignment
    between the operator's own handles renders as ``reassigned`` churn.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415
    from teatree.loop.tick import TickRequest, _identity_aliases_for_request  # noqa: PLC0415

    overlay = MagicMock(spec=OverlayBase)
    overlay.config = MagicMock()
    overlay.config.identity_aliases = []  # no grouped aliases configured
    backend = OverlayBackends(
        name="teatree",
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
        identities=("souliane", "op-gh", "op-gl"),
    )
    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", list)
    assert _identity_aliases_for_request(TickRequest(backends=[backend])) == (("souliane", "op-gh", "op-gl"),)


def test_identity_aliases_for_request_empty_without_backends() -> None:
    from teatree.loop.tick import TickRequest, _identity_aliases_for_request  # noqa: PLC0415

    assert _identity_aliases_for_request(TickRequest()) == ()


def test_identity_aliases_for_request_fails_open_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.loop import tick as tick_mod  # noqa: PLC0415
    from teatree.loop.tick import TickRequest, _identity_aliases_for_request  # noqa: PLC0415

    def _boom(*_args: object, **_kwargs: object) -> object:
        msg = "config read failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(tick_mod, "_identity_alias_groups_for_overlay", _boom)
    request = TickRequest(
        backends=[
            OverlayBackends(
                name="acme",
                hosts=(MagicMock(spec=CodeHostBackend),),
                messaging=None,
                ready_labels=(),
                overlay=None,
            ),
        ],
    )
    assert _identity_aliases_for_request(request) == ()


def test_identity_alias_groups_falls_through_to_toml_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live config → reads ``[overlays.<name>] identity_aliases`` from TOML.

    TOML-only overlays (registered via ``[overlays.<name>]`` without a
    Python class) never populate ``OverlayConfig``; the helper falls back
    to ``discover_overlays`` overrides so the grouping still resolves.
    """
    from teatree.config import OverlayEntry  # noqa: PLC0415
    from teatree.loop.tick import _identity_alias_groups_for_overlay  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    config_path.write_text("[teatree]\n", encoding="utf-8")
    import teatree.config as _config  # noqa: PLC0415

    monkeypatch.setattr("teatree.loop.tick.load_config", lambda: _config.load_config(config_path))
    monkeypatch.setattr(
        "teatree.loop.tick_resolvers.discover_overlays",
        lambda: [
            OverlayEntry(
                name="acme",
                overlay_class="x.y:Z",
                overrides={"identity_aliases": [["acme-gh", "souliane"]]},
            ),
        ],
    )
    assert _identity_alias_groups_for_overlay("acme") == (("acme-gh", "souliane"),)


def test_identity_alias_groups_drops_malformed_inner_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed TOML (non-list group, non-string handles, empty group) → empty.

    A broken config must never crash a tick; the malformed entries are
    silently dropped and the scanner sees the empty-default behaviour.
    """
    from teatree.config import OverlayEntry  # noqa: PLC0415
    from teatree.loop.tick import _identity_alias_groups_for_overlay  # noqa: PLC0415

    monkeypatch.setattr(
        "teatree.loop.tick_resolvers.discover_overlays",
        lambda: [
            OverlayEntry(
                name="acme",
                overlay_class="x.y:Z",
                overrides={
                    "identity_aliases": [
                        "not-a-list",  # dropped
                        [],  # empty group dropped
                        [123, "ok"],  # 123 dropped
                    ],
                },
            ),
        ],
    )
    assert _identity_alias_groups_for_overlay("acme") == (("ok",),)


def test_identity_alias_groups_returns_empty_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``discover_overlays`` raising → the helper degrades silently to ``()``."""
    from teatree.loop.tick import _identity_alias_groups_for_overlay  # noqa: PLC0415

    def _boom() -> object:
        msg = "registry blew up"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.loop.tick_resolvers.discover_overlays", _boom)
    assert _identity_alias_groups_for_overlay("acme") == ()


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


def test_zones_pr_chips_are_bare_no_annotation_decoration() -> None:
    """Per #1377 the chip is just the number — no ``(N notes)`` / ``(pipeline …)``."""
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
    assert "!330" in text
    assert "!99" in text
    # The annotation chunks are gone — bare chips only.
    assert "(7 notes)" not in text, repr(text)
    assert "(pipeline failed)" not in text, repr(text)


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
    # Both tickets fold into exactly one overlay-tagged anchor line (the
    # configured-overlays summary line is a separate, non-ticket anchor).
    ticket_lines = [t for t in anchor_texts if "[acme]" in t]
    assert len(ticket_lines) == 1
    # Terse format (#1377): state-group prefix dropped, all surviving items
    # render flat under the overlay tag.
    assert "#123" in ticket_lines[0]
    assert "#456" in ticket_lines[0]


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


class TestTickReapsStaleClaims(django.test.TestCase):
    def test_run_tick_takes_over_an_orphaned_claim(self) -> None:
        """#652: a tick returns an orphaned in-flight claim to PENDING.

        The session that claimed the Task exited and its lease expired.
        A fresh tick (this one, in another open session) must take the
        orphan over — return it to PENDING so the loop continues — rather
        than FAIL it (which, pre-#652, stalled the loop until a manual
        ``reopen()``). ``reclaim_orphaned_claims`` runs before
        ``reap_stale_claims`` in the tick, so the recoverable orphan is
        reclaimed, not reaped.
        """
        import tempfile  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_tick(TickRequest(scanners=[]), statusline_path=Path(tmp) / "statusline.txt")

        stale.refresh_from_db()
        assert stale.status == Task.Status.PENDING
        assert stale.claimed_by == ""


class TestTickReapsOrphanedReviewingTask(django.test.TransactionTestCase):
    """#998: a tick reaps a PENDING reviewing task when its MR was merged externally.

    End-to-end coverage of the scanner → dispatch → mechanical pipeline:
    a reviewer-role ticket with a PENDING reviewing task whose URL is not
    in the current open-MR scan must have its task completed in the SAME
    tick so ``pending-spawn`` stops re-emitting it.

    Runs the scanner inline (no ``run_tick`` thread pool) so the SQLite
    test backend doesn't deadlock — ``TestCase``'s outer transaction
    locks the table the worker thread tries to read, and the unraisable
    cleanup warning makes the test flaky. The pipeline this validates is
    a flat handler chain: scanner emits → dispatch routes → mechanical
    runs; the unit-level tests for each stage are already in
    ``test_scanners.py`` and ``test_mechanical.py``. This integration
    test wires them together.
    """

    def test_pipeline_completes_pending_reviewing_task_for_missing_mr(self) -> None:
        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415
        from teatree.loop.dispatch import dispatch  # noqa: PLC0415
        from teatree.loop.mechanical import HANDLERS  # noqa: PLC0415
        from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner  # noqa: PLC0415

        # The bug scenario: PENDING task survived the MR's external merge.
        url = "https://gitlab.example.com/x/-/merge_requests/373"
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=url,
            overlay="acme",
            extra={"reviewed_sha": "abc"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Review needed",
        )

        @dataclass(slots=True)
        class _ReviewerHost:
            user_name: str = "alice"

            def current_user(self) -> str:
                return self.user_name

            def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None):
                # API no longer returns the merged MR.
                return []

            def get_review_state(self, *, pr_url: str, reviewer: str):
                from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

                return ReviewState.NONE

            def get_pr_open_state(self, *, pr_url: str):
                # #1074: the forge confirms the MR is genuinely merged, so
                # the orphan sweep is allowed to reap the stale task.
                from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

                return PrOpenState.MERGED

        # Step 1: scanner emits the orphan signal.
        signals = ReviewerPrsScanner(host=_ReviewerHost()).scan()
        assert any(s.kind == "reviewer_pr.task_orphaned" for s in signals)

        # Step 2: dispatch routes to the mechanical handler.
        actions = dispatch(signals)
        orphan_actions = [a for a in actions if a.zone == "reviewer_task_orphaned"]
        assert len(orphan_actions) == 1
        assert orphan_actions[0].kind == "mechanical"

        # Step 3: mechanical handler completes the orphaned task.
        HANDLERS[orphan_actions[0].zone](orphan_actions[0].payload)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestTickReplaysOrphanedTransitions(django.test.TestCase):
    def test_run_tick_replays_a_half_advanced_ticket(self) -> None:
        """#883: a tick recovers a ticket left half-advanced by a crash.

        A coding task COMPLETED but the FSM ``code()`` transition was
        lost to a mid-transition crash, so the ticket is still PLANNED.
        The task is COMPLETED (not CLAIMED) — the claim sweeps cannot see
        it. A fresh tick must run ``replay_orphaned_transitions`` from
        the same boot/tick recovery hook and advance the ticket so the
        loop continues instead of stalling forever.
        """
        import tempfile  # noqa: PLC0415

        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.COMPLETED,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_tick(TickRequest(scanners=[]), statusline_path=Path(tmp) / "statusline.txt")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED

    def test_valueerror_in_one_ticket_does_not_abort_sweep_or_tick(self) -> None:
        """A ValueError-family error from _apply_phase_transition must not crash the whole tick.

        Regression for the factory-wedge class: a shipping task on a REVIEWED ticket
        whose session has no testing/reviewing attestations raises QualityGateError
        (a ValueError subclass) during replay. The old suppress(RuntimeError) does not
        catch it, so the entire _reap_stale_task_claims call — and with it
        reclaim_orphaned_claims + reap_stale_claims — never runs, wedging the loop on
        every tick for as long as the stuck row exists.

        After the fix:
        - the offending ticket's transition error is logged and skipped
        - the healthy ticket's transition still fires (sweep continues)
        - the stale claim sweep still runs (reap_stale_claims executes)
        - run_tick completes without raising
        """
        import tempfile  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        # Stuck ticket: REVIEWED + shipping task COMPLETED but session has no
        # testing/reviewing attestations → _apply_phase_transition raises QualityGateError.
        stuck_ticket = Ticket.objects.create(state=Ticket.State.REVIEWED)
        stuck_session = Session.objects.create(ticket=stuck_ticket, agent_id="ship-agent")
        Task.objects.create(
            ticket=stuck_ticket,
            session=stuck_session,
            phase="shipping",
            status=Task.Status.COMPLETED,
        )

        # Healthy ticket: half-advanced coding task that replay should recover.
        healthy_ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        healthy_session = Session.objects.create(ticket=healthy_ticket, agent_id="code-agent")
        Task.objects.create(
            ticket=healthy_ticket,
            session=healthy_session,
            phase="coding",
            status=Task.Status.COMPLETED,
        )

        # Orphaned claim that reclaim_orphaned_claims must reclaim — verifies the
        # sibling claim sweeps still run even when replay hit an error on the first
        # ticket. reclaim_orphaned_claims runs before reap_stale_claims in the same
        # _reap_stale_task_claims call, so returning this task to PENDING confirms
        # that the claim sweeps were not skipped by the replay error.
        orphan_ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        orphan_session = Session.objects.create(ticket=orphan_ticket, agent_id="stale-agent")
        orphan_task = Task.objects.create(
            ticket=orphan_ticket,
            session=orphan_session,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=timezone.now() - timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Must not raise — the stuck ticket's QualityGateError is isolated.
            run_tick(TickRequest(scanners=[]), statusline_path=Path(tmp) / "statusline.txt")

        # The healthy ticket was advanced by replay despite the error on the stuck one.
        healthy_ticket.refresh_from_db()
        assert healthy_ticket.state == Ticket.State.CODED, (
            f"healthy ticket not advanced — replay aborted early (state={healthy_ticket.state!r})"
        )

        # The stuck ticket stays at REVIEWED — the error was non-fatal and no bad advance happened.
        stuck_ticket.refresh_from_db()
        assert stuck_ticket.state == Ticket.State.REVIEWED, (
            f"stuck ticket unexpectedly advanced to {stuck_ticket.state!r}"
        )

        # The orphaned claim was reclaimed to PENDING — reclaim_orphaned_claims ran
        # after the replay error, confirming the sibling sweeps were not skipped.
        orphan_task.refresh_from_db()
        assert orphan_task.status == Task.Status.PENDING, (
            f"orphaned claim was not reclaimed — sibling sweeps did not run (status={orphan_task.status!r})"
        )
        assert orphan_task.claimed_by == ""


def test_tick_captures_mechanical_handler_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising mechanical handler must not abort the tick — it lands in ``report.errors``."""
    from teatree.loop import mechanical  # noqa: PLC0415

    def boom(_payload: dict) -> None:
        msg = "handler exploded"
        raise RuntimeError(msg)

    monkeypatch.setitem(mechanical.HANDLERS, "ticket_completion", boom)

    signal = ScanSignal(
        kind="ticket.completion_detected",
        summary="ticket 42 ready",
        payload={"ticket_id": 42},
    )
    scanner = _FixedScanner(name="completion", out=[signal])
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)

    assert any("ticket_completion" in label for label in report.errors)
    assert any("handler exploded" in msg for msg in report.errors.values())


def test_tick_captures_persist_agent_dispatches_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``persist_agent_actions`` raises, the tick records it instead of crashing."""

    def boom(_actions: object) -> None:
        msg = "persistence down"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.loop.persistence.persist_agent_actions", boom)

    scanner = _FixedScanner(name="ok", out=[ScanSignal(kind="my_pr.open", summary="x")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[scanner]), statusline_path=statusline)

    assert "dispatch_persist" in report.errors
    assert "persistence down" in report.errors["dispatch_persist"]


def test_tick_multi_overlay_prefixes_summary(tmp_path: Path) -> None:
    """Signals collected via the multi-overlay path get an ``[overlay]`` prefix in the rendered line."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415

    fake_host = MagicMock(spec=CodeHostBackend)
    fake_host.current_user.return_value = "souliane"
    fake_host.list_my_prs.return_value = [
        {"iid": 545, "title": "feat(loop)", "html_url": "https://gh/x/y/pull/545", "user_notes_count": 0}
    ]
    fake_host.list_review_requested_prs.return_value = []
    fake_host.list_assigned_issues.return_value = []
    backends = [OverlayBackends(name="teatree", hosts=(fake_host,), messaging=None, ready_labels=())]

    statusline = tmp_path / "statusline.txt"
    run_tick(TickRequest(backends=backends), statusline_path=statusline, colorize=False)
    contents = statusline.read_text(encoding="utf-8")
    assert "[teatree]" in contents
    # GitHub PR URL → ``#545`` chip glyph (#1377). GitLab URLs use ``!``.
    assert "#545" in contents


def test_repos_from_toml_extracts_path_and_workspace_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teatree.loop.tick import _repos_from_toml  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text(
        '[teatree]\nworkspace_dir = "~/ws"\n'
        '[overlays.acme]\npath = "~/code/acme"\nworkspace_repos = ["acme/api", "acme/web"]\n'
        "[overlays.broken]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    repos = _repos_from_toml()

    assert repos["acme"] == Path.home() / "code" / "acme"
    assert repos["api"].name == "api"
    assert repos["web"].name == "web"


def test_repos_from_toml_returns_empty_on_invalid_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teatree.loop.tick import _repos_from_toml  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text("not = valid = toml = at = all", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _repos_from_toml() == {}


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
    line = _render_pr_group("acme", refs, ticket_index=ticket_index, colorize=True)
    assert "#855:" in line
    assert "!370" in line
    assert "!399" in line


def test_render_pr_group_lists_orphans_when_no_match() -> None:
    from teatree.loop.rendering import _PRRef, _render_pr_group  # noqa: PLC0415

    refs = [_PRRef(iid=42, url="https://example.com/mr/42", annotation="")]
    line = _render_pr_group("t3-teatree", refs, ticket_index={}, colorize=True)
    assert "!42" in line
    assert "#" not in line  # no ticket prefix


def test_render_action_line_inlines_mrs_after_ready_tickets() -> None:
    from teatree.loop.rendering import _IssueRef, _OverlayActionRefs, _PRRef, _render_action_line  # noqa: PLC0415

    pr_refs = [
        _PRRef(iid=370, url="https://gitlab.com/x/y/-/merge_requests/370", annotation=""),
        _PRRef(iid=368, url="https://gitlab.com/x/y/-/merge_requests/368", annotation=""),
    ]
    ready_refs = [
        _IssueRef(label="#855", url="https://gitlab.com/x/y/-/issues/855"),
        _IssueRef(label="#854", url="https://gitlab.com/x/y/-/issues/854"),
    ]
    ticket_index = {
        "https://gitlab.com/x/y/-/merge_requests/370": "855",
        # !368 is an orphan — no parent ticket
    }

    line = _render_action_line(
        "acme",
        _OverlayActionRefs(pr_refs=pr_refs, disposition_refs={}, ready_refs=ready_refs),
        ticket_index=ticket_index,
        colorize=True,
    )

    # !370 should appear inline after #855, !368 should remain in the standalone PR group.
    assert "#855" in line
    assert "!370" in line
    assert "#854" in line
    assert "!368" in line
    pos_855 = line.index("#855")
    pos_370 = line.index("!370")
    assert pos_855 < pos_370, "!370 must follow #855"
    # !370 should not also appear in the standalone PR-group section (de-dup).
    assert line.count("!370") == 1


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
    assert "[acme]" in rendered
    # #1377: the chip is bare — no ``(review)`` annotation suffix.
    assert "(review)" not in rendered, repr(rendered)


class TestLoopOwnerAnchorWiring(django.test.TestCase):
    """``run_tick`` writes the #1073 loop-owner segment on BOTH paths."""

    def test_empty_jobs_path_renders_loop_owner_anchor(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="owner-sess")
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "sl.txt"
            with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "owner-sess"}):
                run_tick(TickRequest(scanners=[]), statusline_path=sl)
            # loop-owner is excluded from the shared consolidated loop line;
            # its badge is rendered per-session in statusline.sh instead
            # (you ✓ / owner·pid / unclaimed). With only the owner lease live,
            # no work loop, and no mini-loop reader injected on this direct
            # ``run_tick`` path, the loop line is intentionally absent.
            body = sl.read_text(encoding="utf-8")
            assert "loops live" not in body, body
            assert "loop running · " not in body, body
            assert "owner" not in body, body

    def test_normal_path_flags_foreign_owner_in_red_zone(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="other-sess")
        scanner = _FixedScanner(name="s", out=[ScanSignal(kind="my_pr.open", summary="x")])
        with tempfile.TemporaryDirectory() as d:
            sl = Path(d) / "sl.txt"
            with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "intruder"}):
                run_tick(TickRequest(scanners=[scanner]), statusline_path=sl, colorize=False)
            assert "loop-owner=session other-se (NOT this session)" in sl.read_text(encoding="utf-8")

    def test_anchor_failure_is_fail_open_no_crash(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "teatree.core.models.LoopLease.objects.ownership_status",
                side_effect=RuntimeError("db down"),
            ),
        ):
            sl = Path(d) / "sl.txt"
            # Must not raise — fail-open like the availability segment.
            run_tick(TickRequest(scanners=[]), statusline_path=sl)
            assert sl.exists()


def _backend_with_overlay(
    *,
    name: str,
    repos: list[str],
    review_channel: tuple[str, str] = ("", ""),
    with_messaging: bool = False,
):
    """Build an :class:`OverlayBackends` with a stub ``OverlayBase`` for wiring tests.

    Returns the backend so the per-overlay scanner builders (#1255,
    #1257) can be exercised through ``build_default_jobs`` without
    spinning up a real overlay package.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata  # noqa: PLC0415

    config = MagicMock(spec=OverlayConfig)
    config.get_review_channel = lambda: review_channel
    config.get_gitlab_token = lambda: ""
    config.get_github_token = lambda: ""
    config.identity_aliases = []
    metadata = MagicMock(spec=OverlayMetadata)
    metadata.get_followup_repos = lambda: list(repos)
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    overlay.metadata = metadata
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=MagicMock(spec=MessagingBackend) if with_messaging else None,
        ready_labels=(),
        overlay=overlay,
    )


def test_build_default_jobs_wires_pr_sweep_per_overlay() -> None:
    """#1257: every overlay with followup repos gets a ``pr_sweep`` job."""
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="teatree", repos=["souliane/teatree"])
    jobs = build_default_jobs(backends=[backend])
    sweep_jobs = [j for j in jobs if j.scanner.name == "pr_sweep"]
    assert len(sweep_jobs) == 1
    assert sweep_jobs[0].overlay == "teatree"
    assert sweep_jobs[0].scanner.repos == ("souliane/teatree",)


def test_build_default_jobs_skips_pr_sweep_when_overlay_has_no_repos() -> None:
    """#1257: an overlay whose ``get_followup_repos`` is empty gets no sweep job."""
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="empty", repos=[])
    jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "pr_sweep"]


def test_build_default_jobs_wires_slack_broadcasts_per_overlay() -> None:
    """#1255: an overlay with messaging + review channel gets one broadcast job."""
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(
        name="teatree",
        repos=["souliane/teatree"],
        review_channel=("the-review-team", "C0DEMOCHAN1"),
        with_messaging=True,
    )
    jobs = build_default_jobs(backends=[backend])
    broadcasts = [j for j in jobs if j.scanner.name == "slack_broadcasts"]
    assert len(broadcasts) == 1
    assert broadcasts[0].overlay == "teatree"
    assert list(broadcasts[0].scanner.channels) == ["C0DEMOCHAN1"]


def test_build_default_jobs_skips_slack_broadcasts_without_review_channel() -> None:
    """#1255: an overlay without a review channel id gets no broadcast job."""
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="teatree", repos=["souliane/teatree"], with_messaging=True)
    jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "slack_broadcasts"]


def test_build_default_jobs_skips_slack_broadcasts_without_messaging() -> None:
    """#1255: an overlay without messaging gets no broadcast job even when channel is set."""
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(
        name="teatree",
        repos=["souliane/teatree"],
        review_channel=("the-review-team", "C0DEMOCHAN1"),
        with_messaging=False,
    )
    jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "slack_broadcasts"]


def test_build_default_jobs_wires_codex_review_when_fleet_doctrine_applies() -> None:
    """#1254: an auto-mode overlay gets a ``codex_review`` scanner."""
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.config import Mode, UserSettings  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="teatree", repos=["souliane/teatree"])
    auto_settings = UserSettings(mode=Mode.AUTO, require_human_approval_to_merge=False)
    with patch("teatree.loop.scanner_factories._effective_settings_for_overlay", return_value=auto_settings):
        jobs = build_default_jobs(backends=[backend])
    codex_jobs = [j for j in jobs if j.scanner.name == "codex_review"]
    assert len(codex_jobs) == 1
    assert codex_jobs[0].overlay == "teatree"
    assert codex_jobs[0].scanner.repos == ("souliane/teatree",)


def test_build_default_jobs_skips_codex_review_when_interactive_mode() -> None:
    """#1254: an interactive-mode overlay is opted out of auto-dispatch."""
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.config import Mode, UserSettings  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="teatree", repos=["souliane/teatree"])
    interactive_settings = UserSettings(mode=Mode.INTERACTIVE)
    with patch("teatree.loop.scanner_factories._effective_settings_for_overlay", return_value=interactive_settings):
        jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "codex_review"]


def test_build_default_jobs_skips_codex_review_when_human_approval_required() -> None:
    """#1254: auto-mode with ``require_human_approval_to_merge`` keeps codex auto-dispatch off.

    The fleet doctrine — auto-codex-on-every-push — applies only when the
    user has opted into end-to-end autonomy. ``require_human_approval_to_merge``
    being on is the user keeping a human-in-the-loop training wheel; the
    scanner respects that and stays silent.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.config import Mode, UserSettings  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="teatree", repos=["souliane/teatree"])
    half_auto = UserSettings(mode=Mode.AUTO, require_human_approval_to_merge=True)
    with patch("teatree.loop.scanner_factories._effective_settings_for_overlay", return_value=half_auto):
        jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "codex_review"]


def test_build_default_jobs_skips_codex_review_when_overlay_has_no_repos() -> None:
    """#1254: an overlay whose ``get_followup_repos`` is empty gets no codex job."""
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.config import Mode, UserSettings  # noqa: PLC0415
    from teatree.loop.tick import build_default_jobs  # noqa: PLC0415

    backend = _backend_with_overlay(name="empty", repos=[])
    auto_settings = UserSettings(mode=Mode.AUTO, require_human_approval_to_merge=False)
    with patch("teatree.loop.scanner_factories._effective_settings_for_overlay", return_value=auto_settings):
        jobs = build_default_jobs(backends=[backend])
    assert not [j for j in jobs if j.scanner.name == "codex_review"]


@dataclass(slots=True)
class _MaintenanceScanner:
    """A stand-in for a maintenance scanner ``sweep_phase`` routes aside."""

    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


def test_run_tick_still_dispatches_sweep_scanner_signals_after_the_split(tmp_path: Path) -> None:
    """#1796: moving ``pr_sweep`` to ``sweep_phase`` must not drop its signal.

    The maintenance scanners run in their own slice now, but their signals
    must still merge before dispatch — behaviour is unchanged.
    """
    sweep = _MaintenanceScanner(name="pr_sweep", out=[ScanSignal(kind="pr_sweep.merged", summary="merged !1")])
    world = _FixedScanner(name="my_prs", out=[ScanSignal(kind="my_pr.open", summary="open")])
    statusline = tmp_path / "statusline.txt"
    report = run_tick(TickRequest(scanners=[sweep, world]), statusline_path=statusline)
    assert report.signal_count == 2
    assert {s.kind for s in report.signals} == {"pr_sweep.merged", "my_pr.open"}


class TestRunTickOrchestrateIsDormant(django.test.TestCase):
    """#1796: ``run_tick`` wires ``orchestrate_phase`` but never claims (dormant)."""

    def test_run_tick_at_full_speed_does_not_claim_pending_tasks(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import Speed, UserSettings  # noqa: PLC0415
        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url="https://x/d", overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="d")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)

        scanner = _FixedScanner(name="s", out=[ScanSignal(kind="my_pr.open", summary="x")])
        with (
            tempfile.TemporaryDirectory() as d,
            patch(
                "teatree.loop.phases.orchestrate.get_effective_settings",
                return_value=UserSettings(speed=Speed.FULL),
            ),
        ):
            run_tick(TickRequest(scanners=[scanner]), statusline_path=Path(d) / "sl.txt")

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_run_tick_survives_an_orchestrate_phase_error(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        scanner = _FixedScanner(name="s", out=[ScanSignal(kind="my_pr.open", summary="x")])
        with (
            tempfile.TemporaryDirectory() as d,
            patch("teatree.loop.tick.orchestrate_phase", side_effect=RuntimeError("config blew up")),
        ):
            sl = Path(d) / "sl.txt"
            report = run_tick(TickRequest(scanners=[scanner]), statusline_path=sl)
            assert sl.exists()
            assert report.signal_count == 1


class TestRunTickOrchestrateClaimToggle(django.test.TestCase):
    """#1796 / agent-teams Track-A PR#1: ``orchestrate_claim_enabled`` arms claim.

    The toggle is read via the existing settings accessor on the dispatch
    wiring. Default OFF keeps the dormant ``claim=False`` path EXACTLY; flipping
    it ON runs ``orchestrate_phase`` with ``claim=True`` so the manifest rows
    the deterministic Python already computes are claimed (the #786-N4 spawn
    boundary).
    """

    def _full_speed_dispatchable_task(self):
        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url="https://x/d", overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="d")
        return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)

    def _run(self, *, toggle: bool):
        import tempfile  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import Speed, UserSettings  # noqa: PLC0415

        settings = UserSettings(speed=Speed.FULL, orchestrate_claim_enabled=toggle)
        with (
            tempfile.TemporaryDirectory() as d,
            patch("teatree.loop.phases.orchestrate.get_effective_settings", return_value=settings),
            patch("teatree.loop.tick.get_effective_settings", return_value=settings),
        ):
            scanner = _FixedScanner(name="s", out=[ScanSignal(kind="my_pr.open", summary="x")])
            run_tick(TickRequest(scanners=[scanner]), statusline_path=Path(d) / "sl.txt")

    def test_toggle_off_keeps_dormant_path_no_claim(self) -> None:
        from teatree.core.models import Task  # noqa: PLC0415

        task = self._full_speed_dispatchable_task()
        self._run(toggle=False)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_toggle_on_claims_the_manifest_rows(self) -> None:
        from teatree.core.models import Task  # noqa: PLC0415

        task = self._full_speed_dispatchable_task()
        self._run(toggle=True)
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
