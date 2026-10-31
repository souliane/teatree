"""The retired t3:orchestrator chaining agent is not described, listed or spawned anywhere.

``t3:orchestrator`` survives only as a dispatch zone that plain code persists. Docs kept
saying an agent of that name chains the phases, and two agent lists kept naming it long
after ``agents/orchestrator.md`` went, so a session reading them looked for an agent that
nothing dispatches. The agent lists are compared with the files that exist, so they fail
for a missing agent as well as a stale one.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conformance._src_tree import REPO_ROOT

ZONE_LABEL = "t3:orchestrator"
WINDOW = 120
CLAIMS_AN_AGENT = re.compile(
    r"\b(?:chains?|chaining|invokes?|spawn(?:s|ed)?)\b|\bagent\s+`t3:orchestrator`", re.IGNORECASE
)
THE_FILE = re.compile(r"agents/orchestrator\b|\borchestrator\.md\b")
THE_CONCEPT = re.compile(r"chaining\s+orchestrator|orchestrator\s+that\s+chains", re.IGNORECASE)
TEXT_SUFFIXES = frozenset({".py", ".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".txt"})
AGENT_NAMES = {path.stem for path in (REPO_ROOT / "agents").glob("*.md")}


def agent_claims(files: dict[str, str]) -> list[str]:
    hits = []
    for name, text in files.items():
        hits.extend(f"{name}: {match.group(0)!r}" for match in THE_FILE.finditer(text))
        hits.extend(f"{name}: {match.group(0)!r}" for match in THE_CONCEPT.finditer(text))
        for zone in re.finditer(re.escape(ZONE_LABEL), text):
            near = text[max(0, zone.start() - WINDOW) : zone.end() + WINDOW]
            hits.extend(f"{name}: {match.group(0)!r}" for match in CLAIMS_AN_AGENT.finditer(near))
    return hits


def tracked_non_test_files() -> dict[str, str]:
    git = shutil.which("git")
    assert git is not None, "git is needed to list the tracked files"
    listing = subprocess.run([git, "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout
    return {
        name: (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        for name in listing.split("\0")
        if name
        and Path(name).suffix in TEXT_SUFFIXES
        and not name.startswith(("tests/", "dev/"))
        and (REPO_ROOT / name).is_file()
    }


def listed_names(text: str, opening: str) -> set[str]:
    listing = text.split(opening, 1)[1].split(")", 1)[0]
    return set(re.findall(r"`([a-z0-9-]+)`", listing))


@pytest.mark.parametrize(
    "stale",
    [
        "the dispatcher routes it to `t3:orchestrator` (chains `coder → tester → reviewer → shipper`)",
        "invoked via the Task tool the same way `t3:orchestrator` invokes them",
        "`pending_task`, `assigned_issue.ready` → agent `t3:orchestrator`",
        "an orchestrator agent defined in agents/orchestrator.md",
        "never to a single chaining orchestrator",
        "never a single orchestrator that chains coder→tester→reviewer→shipper inline",
    ],
)
def test_the_check_sees_each_retired_claim(stale: str) -> None:
    assert agent_claims({"synthetic.md": stale}) != []


def test_the_zone_label_itself_is_not_a_claim() -> None:
    zone_only = "- ``t3:orchestrator`` auto-start actions create a ``Ticket`` plus a planning ``Task``."

    assert agent_claims({"synthetic.md": zone_only}) == []


def test_no_tracked_file_describes_the_retired_chaining_agent() -> None:
    assert agent_claims(tracked_non_test_files()) == []


def test_the_blueprint_agent_list_is_the_agents_that_exist() -> None:
    blueprint = (REPO_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")

    assert listed_names(blueprint, "Phase agents wrap skill bundles (") == AGENT_NAMES


def test_the_map_agent_list_names_only_agents_that_exist() -> None:
    map_text = (REPO_ROOT / "docs" / "MAP.md").read_text(encoding="utf-8")

    assert listed_names(map_text, "Phase sub-agent definitions (") <= AGENT_NAMES


def test_the_loop_topology_appendix_says_the_zone_schedules_planning_first() -> None:
    appendix = (REPO_ROOT / "docs" / "blueprint" / "loop-topology.md").read_text(encoding="utf-8")

    assert (
        '``t3:orchestrator`` auto-start actions create a ``Ticket(role="author")`` plus a ``Task(phase="planning")``'
        in appendix
    )
    assert "which creates the `Ticket` + planning `Task`" in appendix
    assert 'Task(phase="coding")' not in appendix
    assert "+ coding `Task`" not in appendix
