"""Pre-commit hook: auto-generate the tach dependency graph.

Regenerates the Mermaid dependency diagram in docs/dependency-graph.md
whenever ``tach.toml`` or source module structure changes. The diagram
lives outside BLUEPRINT.md so structural growth never inflates the
BLUEPRINT byte-budget corpus.

See: souliane/teatree#1837
"""

import subprocess
from pathlib import Path

_GRAPH_FILE = "docs/dependency-graph.md"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _generate_mermaid() -> str:
    result = subprocess.run(
        ["uv", "run", "tach", "show", "--mermaid", "-o", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _write_graph_file(mermaid: str) -> None:
    graph_path = Path(_GRAPH_FILE)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(
        f"# Module Dependency Graph\n\n```mermaid\n{mermaid}\n```\n",
        encoding="utf-8",
    )


def main() -> int:
    mermaid = _generate_mermaid()
    if not mermaid:
        print("tach show --mermaid produced no output; skipping dependency graph update.")
        return 0

    _write_graph_file(mermaid)
    subprocess.run(["git", "add", _GRAPH_FILE], check=False)
    print(f"Updated dependency graph in {_GRAPH_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
