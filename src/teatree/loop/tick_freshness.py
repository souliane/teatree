"""Repo-freshness snapshot for the statusline header and ``tick-meta.json``.

The ``run_tick`` orchestrator delegates here for everything that
captures "how stale is each repo we track?" — the answer is written
to the sidecar ``tick-meta.json`` so the statusline rendering hook
(and ``t3 loop status``) can show staleness without re-shelling
``git`` at display time.
"""

import datetime as dt
import json
import logging
import os
from pathlib import Path

from teatree.loop.admit_budget import load_meta, meta_path

logger = logging.getLogger(__name__)


def _repo_freshness(repo_path: Path) -> dict[str, int | str] | None:
    """Snapshot a repo's freshness for the statusline header.

    The ``path`` field is included so the statusline hook can recompute
    ``behind`` inline after a ``git pull`` — otherwise the cached value
    stays stale until the next tick (~12 min later).
    """
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: loaded at tick time, not import

    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None
    result = run_allowed_to_fail(
        ["git", "rev-list", "HEAD..origin/main", "--count"],
        cwd=repo_path,
        expected_codes=None,
        timeout=5,
    )
    try:
        behind = int(result.stdout.strip()) if result.returncode == 0 else -1
    except ValueError:
        behind = -1
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_epoch = int(fetch_head.stat().st_mtime) if fetch_head.is_file() else 0
    return {"behind": behind, "fetch_epoch": fetch_epoch, "path": str(repo_path)}


def _repos_from_toml() -> dict[str, Path]:
    """Extract repo paths from the DB overlays registry."""
    from teatree.config import clone_root, load_config  # noqa: PLC0415 — deferred: loaded at tick time, not import

    overlays_cfg = load_config().raw.get("overlays") or {}
    # ``workspace_repos`` resolve to CLONES, so join them under the CLONE root.
    # The legacy ``[teatree] workspace_dir`` key is retired (DB-home now); reading
    # it here would honour a value ignored everywhere else, so route through the
    # one clone-root accessor (env ``T3_WORKSPACE_DIR`` → default ``~/workspace``).
    workspace_dir = clone_root()
    repos: dict[str, Path] = {}
    for name, overlay in overlays_cfg.items():
        if not isinstance(overlay, dict):
            continue
        if "path" in overlay:
            repos[name] = Path(str(overlay["path"])).expanduser()
        for repo_slug in overlay.get("workspace_repos", []):
            if isinstance(repo_slug, str):
                repos[repo_slug.split("/")[-1]] = workspace_dir / repo_slug
    return repos


def _collect_repo_freshness() -> dict[str, dict[str, int | str]]:
    repos: dict[str, Path] = {}
    t3_repo = os.environ.get("T3_REPO")
    if t3_repo:
        repos["t3"] = Path(t3_repo).expanduser()
    repos.update(_repos_from_toml())
    return {label: info for label, path in repos.items() if (info := _repo_freshness(path)) is not None}


def _registered_overlays() -> list[object]:
    """Every registered overlay instance, or ``[]`` on any discovery failure.

    Fail-open so a broken overlay registry never blanks the statusline.
    """
    try:
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: loaded at tick time

        return list(get_all_overlays().values())
    except Exception:
        logger.warning("overlay discovery failed for statusline segments — no overlay segments", exc_info=True)
        return []


def _statusline_segments() -> list[dict[str, str]]:
    """Assemble the contributed inline statusline segments (core + overlays).

    Core contributes the cost chip as the first ``usage``-placed segment; each
    registered overlay then contributes zero or more via
    ``get_statusline_segments``. Every producer fails open to no segment (the
    ``_cost_chip`` contract, extended per overlay), so a broken producer can
    never blank the line. Returns the segments as plain dicts for
    ``tick-meta.json``.
    """
    from teatree.core.statusline_segment import StatuslineSegment  # noqa: PLC0415 — deferred: loaded at tick time

    segments: list[StatuslineSegment] = []
    chip = _cost_chip()
    if chip:
        # The cost chip keeps its neutral (blue) render: color ``None`` falls to
        # the shell's default palette arm, so the migration is byte-for-byte.
        segments.append(StatuslineSegment(id="cost_chip", text=chip, placement="usage"))
    for overlay in _registered_overlays():
        try:
            contributed = overlay.get_statusline_segments()  # ty: ignore[unresolved-attribute]
        except Exception:
            logger.warning("overlay statusline segments producer failed — skipping", exc_info=True)
            continue
        segments.extend(seg for seg in contributed if isinstance(seg, StatuslineSegment))
    return [seg.as_meta() for seg in segments]


def _cost_chip() -> str:
    """The SDK-equivalent cost chip for the sidecar, or ``""`` when silent.

    The statusline header (``hooks/scripts/statusline.sh``) reads this from
    ``tick-meta.json`` and renders it in the usage group right after the
    weekly (``7d=``) rate-limit segment. Computing it here (Python) and
    handing the rendered string to the shell keeps the dollar figure in one
    place. Fails open to ``""`` so a broken cost read never blanks the line.
    """
    from teatree.loop.rendering import cost_chip_lines  # noqa: PLC0415 — deferred: loaded at tick time, not import

    lines = cost_chip_lines()
    return lines[0] if lines else ""


def _write_tick_meta(started_at: dt.datetime, *, target: Path | None = None) -> None:
    from teatree.config import cadence_seconds  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.statusline import default_path  # noqa: PLC0415 — deferred: loaded at tick time, not import

    path = meta_path(target or default_path())
    # #744: the skip path writes tick-meta directly without the
    # render() that side-effect-creates the dir on the normal path —
    # ensure the parent exists so an observability write never crashes
    # the tick (or the skipped-tick freshness touch).
    path.parent.mkdir(parents=True, exist_ok=True)
    # #1036: share the slot's cadence resolver so the displayed next-tick
    # countdown can never diverge from the real loop cadence.
    cadence = cadence_seconds()
    # LOAD-MERGE, never a whole-file write: the freshness header is one of THREE
    # writers into this sidecar. The others — the admission governor's brake state
    # and the orchestrate admit budget — are read by a DIFFERENT process, so
    # overwriting the file leaves a braked governor re-admitting at the high
    # watermark (the hysteresis inert) and the claimer reading UNCLAMPED.
    payload = load_meta(path)
    # ``rendered_at`` is the render-age source the statusline freshness gate
    # (teatree.loop.statusline_staleness + the shell hook) reads to surface a
    # STALE banner when a dead/stopped loop leaves the file frozen. It is the
    # tick's own start epoch — the moment this statusline was produced.
    payload.update(
        {
            "next_epoch": int(started_at.timestamp()) + cadence,
            "cadence": cadence,
            "rendered_at": int(started_at.timestamp()),
            "freshness": _collect_repo_freshness(),
            "segments": _statusline_segments(),
        }
    )
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
