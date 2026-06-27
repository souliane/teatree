"""Tests for ``teatree.loop.phases.render`` — the closing statusline phase."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest

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
