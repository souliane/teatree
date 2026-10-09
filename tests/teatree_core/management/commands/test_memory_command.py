"""``t3 <overlay> memory recall`` — surface cold-tier rules for a query (#2746).

Drives the command through ``call_command`` against a seeded ``--memory-dir`` cold
tier under ``tmp_path``. A relevant query prints the top-K rules, an unrelated query
prints the "no relevant cold-tier entries" line (exit 0), and a missing cold index is
an error (exit 1).
"""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

_COLD_HEADER = "# Auto Memory — Cold Archive Index\n\n> preamble.\n\n"


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY_ARCHIVE.md").write_text(
        _COLD_HEADER
        + "- feedback_worktree_first.md — always create a worktree before editing project files\n"
        + "- feedback_slack_routing.md — route bot DMs only to the user's own channel\n",
        encoding="utf-8",
    )
    return memory


def _run(*args: str) -> str:
    out = StringIO()
    call_command("memory", "recall", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class TestMemoryRecallCommand:
    def test_relevant_query_prints_the_matching_rule(self, memory_dir: Path) -> None:
        out = _run("create a worktree before editing project files", "--memory-dir", str(memory_dir))
        assert "feedback_worktree_first.md" in out
        assert "feedback_slack_routing.md" not in out

    def test_unrelated_query_prints_no_entries_line_exit_zero(self, memory_dir: Path) -> None:
        out = _run("an unrelated question about quantum chromodynamics", "--memory-dir", str(memory_dir))
        assert "no relevant cold-tier entries" in out

    def test_limit_caps_the_number_of_rules(self, tmp_path: Path) -> None:
        memory = tmp_path / "memory"
        memory.mkdir()
        lines = "\n".join(
            f"- feedback_worktree_{i}.md — always create a worktree before editing project file {i}" for i in range(10)
        )
        (memory / "MEMORY_ARCHIVE.md").write_text(_COLD_HEADER + lines + "\n", encoding="utf-8")
        out = _run("create a worktree before editing project files", "--memory-dir", str(memory), "--limit", "2")
        assert sum(1 for line in out.splitlines() if line.strip().startswith("- ")) == 2

    def test_missing_cold_index_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command(
                "memory",
                "recall",
                "anything",
                "--memory-dir",
                str(tmp_path / "absent"),
                stdout=StringIO(),
                stderr=StringIO(),
            )
        assert exc.value.code == 1
