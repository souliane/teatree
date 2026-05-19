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
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)


def _repo_freshness(repo_path: Path) -> dict[str, int | str] | None:
    """Snapshot a repo's freshness for the statusline header.

    The ``path`` field is included so the statusline hook can recompute
    ``behind`` inline after a ``git pull`` — otherwise the cached value
    stays stale until the next tick (~12 min later).
    """
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

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
    """Extract repo paths from ~/.teatree.toml overlays."""
    toml_path = Path.home() / ".teatree.toml"
    if not toml_path.is_file():
        return {}
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    workspace_dir = Path(str(data.get("teatree", {}).get("workspace_dir", "~/workspace"))).expanduser()
    repos: dict[str, Path] = {}
    for name, overlay in (data.get("overlays") or {}).items():
        if not isinstance(overlay, dict):
            continue
        if "path" in overlay:
            repos[name] = Path(str(overlay["path"])).expanduser()
        for repo_slug in overlay.get("workspace_repos", []):
            if isinstance(repo_slug, str):
                repos[repo_slug.split("/")[-1]] = workspace_dir / repo_slug
    return repos


def _canonical_overlay_names() -> dict[str, str]:
    """Map raw ``~/.teatree.toml`` overlay keys to canonical overlay names.

    The toml entry ``[overlays.teatree]`` corresponds to the canonical
    overlay name ``t3-teatree`` — without this mapping the freshness segment
    would label as ``teatree=0`` even though the rest of the statusline tags
    its rows as ``[t3-teatree]``.
    """
    try:
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    canonical = list(get_all_overlays().keys())
    toml_path = Path.home() / ".teatree.toml"
    if not toml_path.is_file():
        return {}
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    mapping: dict[str, str] = {}
    for raw_key in data.get("overlays") or {}:
        if raw_key in canonical:
            continue
        for cname in canonical:
            if cname == raw_key or cname.endswith((f"-{raw_key}", raw_key)):
                mapping[raw_key] = cname
                break
    return mapping


def _collect_repo_freshness() -> dict[str, dict[str, int | str]]:
    repos: dict[str, Path] = {}
    t3_repo = os.environ.get("T3_REPO")
    if t3_repo:
        repos["t3"] = Path(t3_repo).expanduser()
    repos.update(_repos_from_toml())
    aliases = _canonical_overlay_names()
    return {
        aliases.get(label, label): info for label, path in repos.items() if (info := _repo_freshness(path)) is not None
    }


def _write_tick_meta(started_at: dt.datetime, *, target: Path | None = None) -> None:
    from teatree.config import cadence_seconds  # noqa: PLC0415
    from teatree.loop.statusline import default_path  # noqa: PLC0415

    meta_path = (target or default_path()).with_name("tick-meta.json")
    # #744: the skip path writes tick-meta directly without the
    # render() that side-effect-creates the dir on the normal path —
    # ensure the parent exists so an observability write never crashes
    # the tick (or the skipped-tick freshness touch).
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    # #1036: share the slot's cadence resolver so the displayed next-tick
    # countdown can never diverge from the real loop cadence.
    cadence = cadence_seconds()
    next_epoch = int(started_at.timestamp()) + cadence
    freshness = _collect_repo_freshness()
    meta_path.write_text(
        json.dumps({"next_epoch": next_epoch, "cadence": cadence, "freshness": freshness}) + "\n",
        encoding="utf-8",
    )
