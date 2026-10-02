"""A plugin-owned skill demanded under its OWN namespace must stay enforceable.

The write boundary (``normalize_skill_name``) canonicalizes a plugin-owned bare
name UP to ``<namespace>:<name>`` before it reaches ``<session>.pending``, but
``_skill_resolves`` looked for a directory named verbatim ``<namespace>:<name>``.
No such directory exists, so EVERY plugin-owned skill in ``pending`` was
classified unresolvable, warned to stderr, and dropped — the skill-loading gate
could never enforce one. De-qualifying is safe for THIS plugin's namespace only:
it is the exact inverse of the canonicalizer's promotion arm. A FOREIGN
namespace must still be unresolvable (fail open), which is what stops a stale
``old:code`` from being satisfied by an installed bare ``code``.
"""

import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _skill_resolves, handle_enforce_skill_loading


@pytest.fixture
def skills_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A skill tree that is both the plugin-owned set and the resolver search path."""
    skills = tmp_path / "skills"
    for name in ("teatree", "code"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills))
    return skills


class TestPluginNamespacedResolution:
    def test_own_namespace_qualified_name_resolves(self, skills_tree: Path) -> None:
        namespace = router._plugin_namespace()
        assert _skill_resolves(f"{namespace}:teatree", [skills_tree]) is True

    def test_bare_name_still_resolves(self, skills_tree: Path) -> None:
        assert _skill_resolves("teatree", [skills_tree]) is True

    def test_foreign_namespace_stays_unresolvable(self, skills_tree: Path) -> None:
        # Fail-open guard: a stale foreign-namespaced demand must NEVER be
        # satisfied by a same-named skill installed under this plugin.
        assert _skill_resolves("other:teatree", [skills_tree]) is False

    def test_own_namespace_with_no_such_skill_stays_unresolvable(self, skills_tree: Path) -> None:
        namespace = router._plugin_namespace()
        assert _skill_resolves(f"{namespace}:nonexistent", [skills_tree]) is False

    def test_path_shaped_name_is_never_dequalified(self, skills_tree: Path) -> None:
        # A path-shaped ``skill_path`` segment is a literal DIRECTORY name, not
        # a namespaced token, so a stale overlay path must not resolve onto a
        # same-suffixed bare skill.
        namespace = router._plugin_namespace()
        assert _skill_resolves(f"skills/{namespace}:code/SKILL.md", [skills_tree]) is False


class TestGateEnforcesNamespacedPluginSkill:
    """End-to-end: a namespaced plugin skill in ``pending`` blocks code work."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path):
        original = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        yield router.STATE_DIR
        router.STATE_DIR = original

    def test_unloaded_namespaced_plugin_skill_blocks_a_python_edit(
        self, state_dir: Path, skills_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        namespace = router._plugin_namespace()
        (state_dir / "sess-gate.pending").write_text(f"{namespace}:teatree\n", encoding="utf-8")

        blocked = handle_enforce_skill_loading(
            {
                "session_id": "sess-gate",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(skills_tree / "thing.py"), "new_string": "x = 1"},
            }
        )

        assert blocked is True
        captured = capsys.readouterr()
        assert f"{namespace}:teatree" in captured.out + captured.err
        # It must be enforced, never dismissed as an unresolvable stale name.
        assert "unresolvable skill" not in captured.err

    def test_a_foreign_namespaced_demand_still_fails_open(
        self, state_dir: Path, skills_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (state_dir / "sess-foreign.pending").write_text("other:teatree\n", encoding="utf-8")

        blocked = handle_enforce_skill_loading(
            {
                "session_id": "sess-foreign",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(skills_tree / "thing.py"), "new_string": "x = 1"},
            }
        )

        assert blocked is False
        assert "unresolvable skill" in capsys.readouterr().err

    def test_search_dirs_override_is_the_only_source(self, skills_tree: Path) -> None:
        # Guards the fixture itself: the resolver reads the override, so these
        # assertions are about the tmp tree and never about the real install.
        assert os.environ["T3_SKILL_SEARCH_DIRS"] == str(skills_tree)
        assert router._skill_search_dirs() == [skills_tree]
