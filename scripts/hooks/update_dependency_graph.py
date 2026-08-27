"""Pre-commit hook: auto-generate the tach dependency graph.

Regenerates the Mermaid dependency diagram in docs/dependency-graph.md
whenever ``tach.toml`` or source module structure changes. The diagram
lives outside BLUEPRINT.md so structural growth never bloats BLUEPRINT.md.

See: souliane/teatree#1837
"""

import subprocess
import sys
from pathlib import Path

import generated_doc_staging

_GRAPH_FILE = "docs/dependency-graph.md"


class TachShowError(RuntimeError):
    """``tach show`` did not run, so its empty output is not "no graph"."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_tach() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "tach", "show", "--mermaid", "-o", "-"],
        capture_output=True,
        text=True,
        check=False,
    )


def _generate_mermaid() -> str:
    result = _run_tach()
    if result.returncode != 0:
        message = f"tach show --mermaid failed (exit {result.returncode}): {result.stderr.strip()}"
        raise TachShowError(message)
    return result.stdout.strip()


def _write_graph_file(mermaid: str) -> Path | None:
    """Write the graph only when its CONTENT changed; ``None`` when it was already current.

    A generator that rewrites an identical file on every run makes every commit
    that touches ``src/teatree/`` look like it modified a doc, and stages that
    non-change for the committer to explain. Writing only on a real change keeps
    an unchanged graph out of both the tree and the index. Where the staged path
    itself lands is ``generated_doc_staging``'s concern, not this one.
    """
    graph_path = _repo_root() / _GRAPH_FILE
    body = f"# Module Dependency Graph\n\n```mermaid\n{mermaid}\n```\n"
    if graph_path.is_file() and graph_path.read_text(encoding="utf-8") == body:
        return None
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(body, encoding="utf-8")
    return graph_path


def main() -> int:
    try:
        mermaid = _generate_mermaid()
    except TachShowError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not mermaid:
        print("tach show --mermaid produced no output; skipping dependency graph update.")
        return 0

    graph_path = _write_graph_file(mermaid)
    if graph_path is None:
        print(f"Dependency graph already current in {_GRAPH_FILE}")
        return 0
    generated_doc_staging.stage(graph_path)
    print(f"Updated dependency graph in {_GRAPH_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
