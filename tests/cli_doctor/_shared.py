"""Shared staging helpers for the t3 doctor test package.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: the same ``~/.teatree.toml``
writer, home-sandbox stager, and fake-entry-point / editable-map builders
every focused doctor test relies on, relocated so each split module
imports them instead of redefining them.
"""

from pathlib import Path


def _write_teatree_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


def _stage_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolate overlay discovery under ``tmp_path``.

    - Redirects ``Path.home()`` to ``tmp_path`` so ``~/.claude/...`` lookups are sandboxed.
    - Redirects ``teatree.config.CONFIG_PATH`` to ``tmp_path/.teatree.toml``.
    - Muzzles ``importlib.metadata.entry_points`` so installed overlays (``t3-teatree``)
        don't leak into ``discover_overlays()`` / ``discover_active_overlay()``.
    - Moves cwd under ``tmp_path`` so ``_discover_from_manage_py`` cannot climb into
        the real teatree checkout.
    """
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    neutral = tmp_path / "_neutral_cwd"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    return tmp_path


def _fake_entry_point(dist_name: str = "my-overlay") -> object:
    """Return a fake ``importlib.metadata.EntryPoint`` with ``dist.name``.

    A real ``EntryPoint`` also carries a ``value`` (the overlay class path),
    which overlay discovery reads (``discover_overlays``); the fake provides a
    plausible one so the entry-point branch resolves without an attribute error.
    """
    dist = type("_FakeDist", (), {"name": dist_name})()
    return type(
        "_FakeEP",
        (),
        {"name": f"t3-{dist_name}", "value": f"{dist_name}.overlay:Overlay", "dist": dist},
    )()


def _editable_map(**dists: tuple[bool, str]):
    """Build an ``editable_info`` side_effect from a ``dist_name -> (editable, url)`` map."""

    def side_effect(dist_name: str) -> tuple[bool, str]:
        return dists.get(dist_name, (False, ""))

    return side_effect
