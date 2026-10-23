import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def test_locked_pydantic_packages_match_the_declared_floors() -> None:
    manifest = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(manifest["project"]["dependencies"])
    assert "pydantic>=2.14.1,<3" in dependencies
    assert "pydantic-settings>=2.16" in dependencies
    assert "pydantic-ai-slim[anthropic,openai]>=2.56,<3" in dependencies

    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = {package["name"]: package["version"] for package in lock["package"]}
    assert {
        name: versions[name]
        for name in (
            "pydantic",
            "pydantic-core",
            "pydantic-settings",
            "pydantic-ai-slim",
            "pydantic-graph",
        )
    } == {
        "pydantic": "2.14.1",
        "pydantic-core": "2.50.1",
        "pydantic-settings": "2.16.0",
        "pydantic-ai-slim": "2.56.0",
        "pydantic-graph": "2.56.0",
    }
