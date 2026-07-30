"""Tests for ``teatree.loop.phases.render`` — the closing statusline phase."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import PullRequest, Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases import render_phase
from teatree.loop.phases.render import _identity_aliases_for_request
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport, TickRequest

_NOW = dt.datetime(2026, 6, 16, tzinfo=dt.UTC)


@dataclass(slots=True)
class _NamedScanner:
    name: str

    def scan(self) -> list[ScanSignal]:
        return []


def _job(name: str) -> _ScannerJob:
    return _ScannerJob(scanner=_NamedScanner(name=name), overlay="")


def test_render_phase_is_a_named_loop_phase() -> None:
    from teatree.loop import phases  # noqa: PLC0415

    assert "render_phase" in phases.__all__


def test_render_phase_writes_active_statusline_and_sidecars(tmp_path: Path) -> None:
    sl = tmp_path / "statusline.txt"
    report = TickReport(started_at=_NOW, signals=[ScanSignal(kind="my_pr.open", summary="x")])
    report.actions = dispatch(report.signals, errors=report.errors)

    render_phase(
        report,
        TickRequest(),
        jobs=[_job("my_prs")],
        statusline_path=sl,
        colorize=False,
    )

    assert report.statusline_path == sl
    assert sl.is_file()
    assert sl.with_name("tick-meta.json").is_file()
    assert sl.with_name("open-prs.json").is_file()


def test_render_phase_surfaces_scanner_errors_in_active_render(tmp_path: Path) -> None:
    sl = tmp_path / "statusline.txt"
    report = TickReport(started_at=_NOW, errors={"my_prs": "boom"})

    render_phase(report, TickRequest(), jobs=[_job("my_prs")], statusline_path=sl, colorize=False)

    assert "scanner errors: my_prs" in sl.read_text(encoding="utf-8")


class TestRenderPhaseReconcilesManualPrs(TestCase):
    """render_phase wires in the #1912 manual-MR reconciler (after the scan)."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.sl = tmp_path / "statusline.txt"

    def test_render_phase_reconciles_manual_prs_into_rows(self) -> None:
        from teatree.core.models.pull_request import PullRequest  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/855",
            state="started",
        )
        url = "https://github.com/souliane/teatree/pull/370"
        report = TickReport(
            started_at=_NOW,
            signals=[
                ScanSignal(
                    kind="my_pr.open",
                    summary="PR #370",
                    payload={"url": url, "iid": 370, "raw": {"description": "Closes #855"}},
                )
            ],
        )
        report.actions = dispatch(report.signals, errors=report.errors)

        render_phase(report, TickRequest(), jobs=[_job("my_prs")], statusline_path=self.sl, colorize=False)

        assert PullRequest.objects.filter(url=url).count() == 1


class TestRenderPhaseReconcilesMergedTickets(TestCase):
    """render_phase drives a stranded pre-merged ticket to MERGED (#3540)."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.sl = tmp_path / "statusline.txt"

    def test_author_not_started_with_merged_pr_reaches_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        PullRequest.objects.create(
            ticket=ticket,
            url="https://github.com/souliane/teatree/pull/3540",
            repo="souliane/teatree",
            iid="3540",
            overlay="test",
            state=PullRequest.State.MERGED,
        )
        report = TickReport(started_at=_NOW, signals=[])
        report.actions = dispatch(report.signals, errors=report.errors)

        render_phase(report, TickRequest(), jobs=[], statusline_path=self.sl, colorize=False)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED


def test_render_phase_idle_renders_statusline_without_jobs(tmp_path: Path) -> None:
    sl = tmp_path / "statusline.txt"
    report = TickReport(started_at=_NOW)

    render_phase(report, TickRequest(), jobs=[], statusline_path=sl, colorize=False)

    assert report.statusline_path == sl
    assert sl.is_file()
    assert sl.with_name("tick-meta.json").is_file()


def test_render_phase_idle_omits_scanner_error_line(tmp_path: Path) -> None:
    sl = tmp_path / "statusline.txt"
    report = TickReport(started_at=_NOW, errors={"my_prs": "boom"})

    render_phase(report, TickRequest(), jobs=[], statusline_path=sl, colorize=False)

    assert "scanner errors" not in sl.read_text(encoding="utf-8")


def test_rerender_statusline_rewrites_a_stale_file(tmp_path: Path) -> None:
    """The #2625 self-heal seam re-renders the zones file from current state.

    ``StaleStatuslineEntryDetector``'s auto-fix wires its ``rerender`` callable
    to this seam: a merged-PR / terminal-ticket URL the detector flags must drop
    out of the rendered file when the seam runs — the retired ``_default_rerender``
    no-op left the stale URL in place.
    """
    from teatree.loop.phases.render import rerender_statusline  # noqa: PLC0415

    sl = tmp_path / "statusline.txt"
    sl.write_text("stale merged-PR https://github.com/acme/repo/pull/1\n", encoding="utf-8")

    out = rerender_statusline(sl, colorize=False)

    assert out == sl
    assert "stale merged-PR" not in sl.read_text(encoding="utf-8")


def test_rerender_statusline_preserves_the_open_prs_cache(tmp_path: Path) -> None:
    """A re-render must NOT wipe the open-PRs snapshot a real scan recorded (M5).

    ``rerender_statusline`` is a display refresh with no scan, so it carries no
    fresh ``my_pr.*`` signals. The prior behaviour wrote an EMPTY cache, which
    destroyed every open PR a previous full tick had snapshotted and blanked the
    anchor until the next scan. The cache is owned by the scan path: the refresh
    must leave it intact and re-render the preserved PRs from it.
    """
    from teatree.loop.open_prs import OpenPr, read_open_prs_cache, write_open_prs_cache  # noqa: PLC0415
    from teatree.loop.phases.render import rerender_statusline  # noqa: PLC0415

    sl = tmp_path / "statusline.txt"
    pr = OpenPr(
        iid=42,
        title="ship the thing",
        url="https://github.com/acme/repo/pull/42",
        overlay="teatree",
        draft=False,
    )
    write_open_prs_cache([pr], statusline_path=sl)

    rerender_statusline(sl, colorize=False)

    # The seeded snapshot survives the re-render (the bug wiped it to []).
    assert read_open_prs_cache(statusline_path=sl) == [pr]
    # And the preserved PR is actually rendered into the statusline anchor.
    rendered = sl.read_text(encoding="utf-8")
    assert "#42" in rendered
    assert "ship the thing" in rendered


def test_self_improve_rerender_adapter_invokes_the_render_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The action-ladder ``auto_fix_callable`` adapter bridges to ``rerender_statusline``.

    The dedicated ``loop_self_improve`` slot injects this as the ladder's
    ``auto_fix_callable`` — the sole live orchestration entry point since the
    master-tick removal (#2650).
    """
    from teatree.loop.phases import render as render_module  # noqa: PLC0415

    calls: list[object] = []
    monkeypatch.setattr(render_module, "rerender_statusline", lambda *a, **k: calls.append((a, k)))

    render_module.self_improve_rerender(object())

    assert len(calls) == 1


def test_rerender_statusline_defaults_target_to_the_canonical_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no target the seam writes the canonical statusline path (idle render)."""
    from teatree.loop.phases import render as render_module  # noqa: PLC0415

    written: dict[str, object] = {}

    def _fake_render(zones: object, *, target: Path | None = None, colorize: bool | None = None) -> Path:
        written["target"] = target
        return Path("/tmp/sentinel-statusline.txt")

    monkeypatch.setattr(render_module, "render", _fake_render)
    out = render_module.rerender_statusline()

    assert out == Path("/tmp/sentinel-statusline.txt")
    assert written["target"] is None


def test_identity_aliases_for_request_unions_across_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

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


def test_identity_aliases_for_request_falls_back_to_operator_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    overlay = MagicMock(spec=OverlayBase)
    overlay.config = MagicMock()
    overlay.config.identity_aliases = []
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
    assert _identity_aliases_for_request(TickRequest()) == ()


def test_identity_aliases_for_request_fails_open_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
    from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415
    from teatree.loop.phases import render as render_mod  # noqa: PLC0415

    def _boom(*_args: object, **_kwargs: object) -> object:
        msg = "config read failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(render_mod, "_identity_groups_for_overlay", _boom)
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
