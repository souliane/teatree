"""Pre-commit hook: auto-generate the tach dependency graph.

Regenerates the Mermaid dependency diagram in BLUEPRINT.md whenever
``tach.toml`` or source module structure changes.

See: souliane/teatree#197
"""

import re
import subprocess
from pathlib import Path

_BLUEPRINT = Path("BLUEPRINT.md")
_MARKER_START = "<!-- tach-dependency-graph:start -->"
_MARKER_END = "<!-- tach-dependency-graph:end -->"


def _generate_mermaid() -> str:
    result = subprocess.run(
        ["uv", "run", "tach", "show", "--mermaid", "-o", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _update_blueprint(mermaid: str) -> bool:
    if not _BLUEPRINT.is_file():
        return False

    content = _BLUEPRINT.read_text(encoding="utf-8")

    block = f"{_MARKER_START}\n\n```mermaid\n{mermaid}\n```\n\n{_MARKER_END}"

    pattern = re.compile(
        re.escape(_MARKER_START) + r".*?" + re.escape(_MARKER_END),
        re.DOTALL,
    )

    if pattern.search(content):
        new_content = pattern.sub(block, content)
    else:
        new_content = content.rstrip() + f"\n\n## Module Dependency Graph\n\n{block}\n"

    if new_content == content:
        return False

    _BLUEPRINT.write_text(new_content, encoding="utf-8")
    return True


def main() -> int:
    mermaid = _generate_mermaid()
    if not mermaid:
        print("tach show --mermaid produced no output; skipping dependency graph update.")
        return 0

    if _update_blueprint(mermaid):
        subprocess.run(["git", "add", str(_BLUEPRINT)], check=False)
        print(f"Updated dependency graph in {_BLUEPRINT}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
