"""Tests for teatree.skill_deps — transitive dependency resolution."""

import pytest

from teatree.skill_deps import resolve_all, resolve_requires


def _index(*entries: tuple[str, list[str]]) -> list[dict[str, object]]:
    """Build a minimal trigger index from (skill, requires) tuples."""
    return [{"skill": name, "requires": deps} for name, deps in entries]


class TestResolveRequires:
    def test_no_deps(self):
        index = _index(("ship", []))
        assert resolve_requires(["ship"], index) == ["ship"]

    def test_linear_chain(self):
        index = _index(("rules", []), ("workspace", ["rules"]), ("code", ["workspace"]))
        assert resolve_requires(["code"], index) == ["rules", "workspace", "code"]

    def test_diamond(self):
        index = _index(
            ("rules", []),
            ("workspace", ["rules"]),
            ("platforms", ["rules"]),
            ("review", ["workspace", "platforms"]),
        )
        result = resolve_requires(["review"], index)
        assert result == ["rules", "workspace", "platforms", "review"]

    def test_cycle_raises(self):
        index = _index(("a", ["b"]), ("b", ["a"]))
        with pytest.raises(ValueError, match="Circular dependency"):
            resolve_requires(["a"], index)

    def test_self_reference_raises(self):
        index = _index(("a", ["a"]))
        with pytest.raises(ValueError, match="Circular dependency"):
            resolve_requires(["a"], index)

    def test_unknown_skill_passes_through(self):
        index = _index(("rules", []))
        result = resolve_requires(["ac-django", "rules"], index)
        assert "ac-django" in result
        assert "rules" in result

    def test_empty_input(self):
        assert resolve_requires([], []) == []

    def test_multiple_skills_dedup_deps(self):
        index = _index(
            ("rules", []),
            ("workspace", ["rules"]),
            ("code", ["workspace"]),
            ("test", ["workspace"]),
        )
        result = resolve_requires(["code", "test"], index)
        assert result.count("rules") == 1
        assert result.count("workspace") == 1
        # Both code and test present.
        assert "code" in result
        assert "test" in result
        # Deps come before dependents.
        assert result.index("rules") < result.index("workspace")
        assert result.index("workspace") < result.index("code")
        assert result.index("workspace") < result.index("test")

    def test_real_teatree_graph(self):
        index = _index(
            ("rules", []),
            ("workspace", ["rules"]),
            ("platforms", []),
            ("code", ["workspace"]),
            ("review", ["workspace", "platforms", "code"]),
            ("ship", ["workspace", "rules"]),
        )
        result = resolve_requires(["review"], index)
        assert result[0] == "rules"
        assert result[-1] == "review"
        assert result.index("workspace") < result.index("code")
        assert result.index("code") < result.index("review")


class TestResolveAll:
    def test_precomputes_all_skills(self):
        index = _index(
            ("rules", []),
            ("workspace", ["rules"]),
            ("code", ["workspace"]),
        )
        result = resolve_all(index)
        assert result["rules"] == ["rules"]
        assert result["workspace"] == ["rules", "workspace"]
        assert result["code"] == ["rules", "workspace", "code"]

    def test_cycle_returns_skill_alone(self):
        index = _index(("a", ["b"]), ("b", ["a"]))
        result = resolve_all(index)
        assert result["a"] == ["a"]
        assert result["b"] == ["b"]
