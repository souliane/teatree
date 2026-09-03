# test-path: cross-cutting — a whole-tree vocabulary invariant; no src/teatree/ mirror.
"""One name for the ``<session>.agents`` state file: the dispatch ledger (#4131).

``hooks/`` called it a *roster* while BLUEPRINT called it a *dispatch ledger*, and
nothing noticed: the only guard on the word (``test_roster_mentions_are_retirement_framed``)
scans BLUEPRINT's loop-topology section for the RETIRED immortal-singleton loop
roster and never looks at ``hooks/``. So the writer and the reader of the same
bytes sat on opposite sides of a naming line.

This walks both surfaces. ``roster`` stays legal for the three unrelated concepts
that own it — the retired loop roster, the Agent-Teams mates roster, the
cache-reset/registry-parity rosters — because the anchor here is the state file
itself, not the bare word.
"""

import subprocess
from pathlib import Path

import pytest

from tests._generated_artifacts import DURATIONS_CASSETTE
from tests._git_repo import make_git_repo, run_git

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "hooks" / "scripts"
_BLUEPRINT = _REPO_ROOT / "BLUEPRINT.md"

#: How the state file is written in prose. Anchoring on this rather than on the bare
#: ``.agents`` suffix keeps the walk off ``~/.agents/skills`` and the ``"agents"``
#: state-file-suffix literals, which name other things entirely.
_ANCHOR = "<session>.agents"

#: Chars either side of an anchor that count as "describing the state file". Wide
#: enough to span a docstring sentence that wraps across lines, narrow enough that an
#: unrelated paragraph cannot bleed in.
_RADIUS = 240

#: The retired module name. Its ``sys.modules`` alias strings are load-bearing — a
#: hook that cannot import fails OPEN, so a half-renamed alias disables the capture
#: silently rather than erroring.
_RETIRED_MODULE = "agent_roster"


def _windows(text: str, anchor: str, radius: int) -> list[str]:
    """Every ``±radius`` window around an ``anchor`` occurrence, lowercased."""
    low = text.lower()
    found: list[str] = []
    start = 0
    while (i := low.find(anchor.lower(), start)) != -1:
        found.append(low[max(0, i - radius) : i + len(anchor) + radius])
        start = i + 1
    return found


def _tracked_files(repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Tracked files, minus this one — the guard must name what it forbids."""
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — repo-relative git, no user input
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    # The durations cassette goes too: it records node ids, so it spells the retired
    # module without referencing it.
    exempt = {Path(__file__).resolve(), (repo_root / DURATIONS_CASSETTE).resolve()}
    tracked = (repo_root / line for line in out.stdout.splitlines() if line)
    return [path for path in tracked if path.resolve() not in exempt]


def _hook_files_naming_the_state_file() -> list[Path]:
    """Every tracked file under ``hooks/`` that describes the state file.

    Whole-dir rather than ``scripts/*.py``: ``hooks/CLAUDE.md`` is the conventions
    doc a hook author reads first, so a split there is exactly as costly as one in
    the module docstring.
    """
    hooks_dir = _REPO_ROOT / "hooks"
    return sorted(
        path
        for path in _tracked_files()
        if hooks_dir in path.parents and path.is_file() and _ANCHOR in path.read_text(encoding="utf-8", errors="ignore")
    )


class TestWindowsHelper:
    """The predicate the walks below rest on — anti-vacuity for the scanner itself."""

    def test_every_occurrence_gets_its_own_window(self) -> None:
        assert len(_windows("x A y A z", "a", radius=1)) == 2

    def test_a_window_is_clipped_at_the_text_edges(self) -> None:
        assert _windows("ab", "a", radius=99) == ["ab"]

    def test_an_absent_anchor_yields_nothing(self) -> None:
        assert _windows("nothing here", "<session>.agents", radius=10) == []


class TestTheTrackedWalkExemptsTheGeneratedCassette:
    """Control: exactly one generated file is exempt, not the directory holding it."""

    def test_the_cassette_is_skipped_while_its_neighbour_is_still_walked(self, tmp_path: Path) -> None:
        make_git_repo(tmp_path)
        for name in (DURATIONS_CASSETTE, "dev/handwritten.py"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{_RETIRED_MODULE} = 1\n")
        run_git(tmp_path, "add", "-A")
        walked = {p.relative_to(tmp_path).as_posix() for p in _tracked_files(tmp_path)}
        assert DURATIONS_CASSETTE not in walked
        assert "dev/handwritten.py" in walked


class TestModuleIsNamedForTheLedger:
    def test_the_sibling_lives_at_its_ledger_name(self) -> None:
        assert (_SCRIPTS_DIR / "dispatch_ledger.py").is_file()

    def test_the_roster_named_sibling_is_gone(self) -> None:
        assert not (_SCRIPTS_DIR / f"{_RETIRED_MODULE}.py").exists()

    def test_no_tracked_file_still_names_the_retired_module(self) -> None:
        # Catches a stale ``sys.modules`` alias string, a stale import, a stale
        # patch target, and a stale reference in config or docs — none of which
        # lint can see, and the alias ones fail OPEN rather than loudly.
        stale = [
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _tracked_files()
            if path.is_file() and _RETIRED_MODULE in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert stale == [], f"stale '{_RETIRED_MODULE}' reference(s): {stale}"


class TestHooksDescribeTheLedgerAsALedger:
    def test_the_walk_still_finds_the_hooks_that_describe_it(self) -> None:
        # Without this the parametrized guard below silently degrades to zero cases
        # the moment the anchor spelling drifts, and reads green having checked nothing.
        assert {p.name for p in _hook_files_naming_the_state_file()} >= {"dispatch_ledger.py", "resume_admission.py"}

    @pytest.mark.parametrize("path", _hook_files_naming_the_state_file(), ids=lambda p: p.name)
    def test_no_state_file_mention_calls_it_a_roster(self, path: Path) -> None:
        offenders = [w for w in _windows(path.read_text(encoding="utf-8"), _ANCHOR, _RADIUS) if "roster" in w]
        assert offenders == [], f"{path.name} calls {_ANCHOR} a roster: …{offenders[0]}…"

    def test_the_ledger_module_says_ledger(self) -> None:
        text = (_SCRIPTS_DIR / "dispatch_ledger.py").read_text(encoding="utf-8").lower()
        assert "ledger" in text


class TestBlueprintAgreesWithHooks:
    def test_blueprint_calls_it_a_dispatch_ledger(self) -> None:
        windows = _windows(_BLUEPRINT.read_text(encoding="utf-8"), _ANCHOR, _RADIUS)
        assert windows, f"BLUEPRINT no longer describes {_ANCHOR}"
        assert any("dispatch ledger" in w for w in windows)

    def test_blueprint_never_calls_it_a_roster(self) -> None:
        offenders = [w for w in _windows(_BLUEPRINT.read_text(encoding="utf-8"), _ANCHOR, _RADIUS) if "roster" in w]
        assert offenders == [], f"BLUEPRINT calls {_ANCHOR} a roster: …{offenders[0]}…"
