import json
from pathlib import Path

import pytest

from teatree.cli.setup import codex_plugin_payload
from teatree.cli.setup.codex_plugin_payload import CodexPayloadError, CodexPluginPayload


def _manifest_root(root: Path, **plugin_fields: object) -> Path:
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps({"name": "souliane", "plugins": [{"name": "t3", "source": {"path": "./plugins/t3"}}]})
    )
    (root / ".codex-plugin").mkdir()
    fields = {"name": "t3", "skills": "./skills/", "mcpServers": "./.mcp.json", "homepage": "https://example.org"}
    (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({**fields, **plugin_fields}))
    (root / "skills" / "code").mkdir(parents=True)
    (root / "skills" / "code" / "SKILL.md").write_text("# code\n")
    (root / ".mcp.json").write_text("{}\n")
    return root


def _checkout_with_junk(root: Path) -> Path:
    _manifest_root(root)
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "pack").write_bytes(b"x" * 1024)
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.so").write_bytes(b"\0" * (5 * 1024 * 1024))
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("")
    (root / "plugins").mkdir()
    (root / "plugins" / "t3").symlink_to("..")
    return root


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_slim_tree_holds_only_the_manifest_declared_payload(tmp_path: Path) -> None:
    checkout = _checkout_with_junk(tmp_path / "checkout")

    built = CodexPluginPayload(checkout, tmp_path / "codex-plugin").materialize()

    assert _tree(built) == {
        ".agents/plugins/marketplace.json",
        "plugins/t3/.codex-plugin/plugin.json",
        "plugins/t3/skills/code/SKILL.md",
        "plugins/t3/.mcp.json",
    }
    assert not [path for path in built.rglob("*") if path.is_symlink()]
    assert sum(path.stat().st_size for path in built.rglob("*") if path.is_file()) < 1024 * 1024


def test_rematerialising_drops_a_deleted_skill(tmp_path: Path) -> None:
    root = _manifest_root(tmp_path / "root")
    (root / "skills" / "gone").mkdir()
    (root / "skills" / "gone" / "SKILL.md").write_text("# gone\n")
    payload = CodexPluginPayload(root, tmp_path / "codex-plugin")
    payload.materialize()

    (root / "skills" / "gone" / "SKILL.md").unlink()
    (root / "skills" / "gone").rmdir()
    built = payload.materialize()

    assert "plugins/t3/skills/gone/SKILL.md" not in _tree(built)
    assert "plugins/t3/skills/code/SKILL.md" in _tree(built)


@pytest.mark.parametrize("declared", ["./", ".", "../outside", "/etc"])
def test_a_declared_path_at_or_outside_the_root_is_refused(tmp_path: Path, declared: str) -> None:
    root = _manifest_root(tmp_path / "root", skills=declared)
    (tmp_path / "outside").mkdir()

    with pytest.raises(CodexPayloadError):
        CodexPluginPayload(root, tmp_path / "codex-plugin").materialize()

    assert not (tmp_path / "codex-plugin").exists()


def test_a_symlinked_directory_inside_the_payload_is_refused(tmp_path: Path) -> None:
    root = _manifest_root(tmp_path / "root")
    (root / "skills" / "loop").symlink_to("..")

    with pytest.raises(CodexPayloadError, match="symlinked directory"):
        CodexPluginPayload(root, tmp_path / "codex-plugin").materialize()


def test_a_payload_over_the_cap_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _manifest_root(tmp_path / "root")
    monkeypatch.setattr(codex_plugin_payload, "PAYLOAD_MAX_BYTES", 4)

    with pytest.raises(CodexPayloadError, match="cap"):
        CodexPluginPayload(root, tmp_path / "codex-plugin").materialize()

    assert not (tmp_path / "codex-plugin").exists()


def test_a_failed_rebuild_leaves_the_previous_tree_in_place(tmp_path: Path) -> None:
    root = _manifest_root(tmp_path / "root")
    payload = CodexPluginPayload(root, tmp_path / "codex-plugin")
    payload.materialize()

    (root / ".mcp.json").unlink()
    with pytest.raises(CodexPayloadError):
        payload.materialize()

    assert "plugins/t3/.mcp.json" in _tree(tmp_path / "codex-plugin")
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".codex-plugin")] == []
