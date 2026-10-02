"""Tests for ``t3 ui`` — the trogon-backed command browser.

Covers the completeness oracle (trogon's introspection sees the full
command surface in the SSOT), a headless pilot smoke test (the TUI mounts
and renders its command tree), and the optional-extra ImportError guard.
"""

import asyncio
import builtins
from unittest.mock import patch

import click
from typer.main import get_command, get_group
from typer.testing import CliRunner

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_paths

runner = CliRunner()


def _raw_click_paths(base_name: str = "t3") -> set[str]:
    """Command paths as trogon's introspection walks them — no proxy-leaf resolution.

    Mirrors ``command_paths`` minus the ``_resolve_proxy_leaf`` step: overlay
    bridge subcommands stay leaves, so their underlying Django subcommands are
    not descended into (the documented v1 limitation).
    """
    paths: set[str] = set()

    def _collect(cmd: click.Command, parts: list[str]) -> None:
        paths.add(" ".join(parts))
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd, info_name=parts[-1])
            for sub_name in cmd.list_commands(ctx):
                sub_cmd = cmd.get_command(ctx, sub_name)
                if sub_cmd is not None:
                    _collect(sub_cmd, [*parts, sub_name])

    _collect(get_command(app), [base_name])
    return paths


def _introspected_paths() -> set[str]:
    from trogon.introspect import introspect_click_app  # noqa: PLC0415

    schemas = introspect_click_app(get_group(app))
    root = next(iter(schemas.values()))

    def _walk(subcommands: dict, prefix: list[str]) -> set[str]:
        out: set[str] = set()
        for name, schema in subcommands.items():
            path = [*prefix, str(name)]
            out.add(" ".join(path))
            out |= _walk(schema.subcommands, path)
        return out

    return {"t3"} | _walk(root.subcommands, ["t3"])


class TestUiCompletenessOracle:
    def test_ui_is_a_real_command_path(self):
        register_overlay_commands(allowlist={"t3-teatree"})
        assert "t3 ui" in command_paths(app)

    def test_introspection_covers_full_navigable_surface(self):
        register_overlay_commands(allowlist={"t3-teatree"})
        assert _introspected_paths() == _raw_click_paths()

    def test_introspection_misses_only_overlay_proxy_leaf_children(self):
        register_overlay_commands(allowlist={"t3-teatree"})
        uncovered = command_paths(app) - _introspected_paths()
        assert uncovered == {p for p in uncovered if " ticket context " in p}
        assert uncovered, "expected the documented proxy-leaf gap to be non-empty"


class TestUiHeadlessPilot:
    def test_tui_mounts_command_tree(self):
        register_overlay_commands(allowlist={"t3-teatree"})
        from trogon.trogon import Trogon  # noqa: PLC0415
        from trogon.widgets.command_tree import CommandTree  # noqa: PLC0415

        async def _mount_and_query() -> int:
            tui = Trogon(get_group(app), app_name="t3")
            async with tui.run_test() as pilot:
                return len(pilot.app.query(CommandTree))

        assert asyncio.run(_mount_and_query()) >= 1


class TestUiLaunch:
    def test_launches_trogon_over_the_full_app(self):
        import trogon.trogon as trogon_mod  # noqa: PLC0415

        seen: dict[str, object] = {}
        real_init = trogon_mod.Trogon.__init__

        def spy_init(self, cli, app_name=None, **kwargs):
            seen["app_name"] = app_name
            seen["is_group"] = isinstance(cli, click.Group)
            real_init(self, cli, app_name=app_name, **kwargs)

        with (
            patch.object(trogon_mod.Trogon, "run", lambda self, *a, **k: None),
            patch.object(trogon_mod.Trogon, "__init__", spy_init),
            patch("teatree.cli._maybe_show_update_notice"),
        ):
            result = runner.invoke(app, ["ui"])

        assert result.exit_code == 0
        assert seen == {"app_name": "t3", "is_group": True}


class TestUiImportGuard:
    def test_missing_extra_exits_with_guidance(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "trogon.trogon" or name.startswith("trogon."):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with patch("teatree.cli._maybe_show_update_notice"):
            result = runner.invoke(app, ["ui"])
        assert result.exit_code == 1
        assert "ui" in result.output
        assert "uv sync --group ui" in result.output
