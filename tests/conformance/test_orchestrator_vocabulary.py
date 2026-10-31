"""Shrink-only census of the word "orchestrator": it names the deterministic control plane.

Every tracked file naming the word is assigned one class (N1 to N10) by an ordered rule in
``orchestrator_vocabulary.toml`` and carries a per-file allowance that can only shrink. Three more
families share the ledger: identifier forms confined to a listed file set, a closed list of phrases that
call an agent or the attended seat "the orchestrator", and structural senses that must have no hits.

Freeze the ledger. It lowers counts and drops emptied files; it never raises a file it was not told to:

    uv run --no-sync python -m tests.conformance.test_orchestrator_vocabulary --freeze
    uv run --no-sync python -m tests.conformance.test_orchestrator_vocabulary --freeze --raise PATH [--raise PATH ...]

The first form lists, on stderr, each file it refused to raise or to add as a ready-made ``--raise`` flag,
and each file no rule classifies: that one needs a ``[[rule]]`` in the toml, which ``--raise`` cannot supply.
"""

import argparse
import dataclasses
import json
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

import pytest

from teatree.utils.run import CommandFailedError, run_checked
from tests.quality._deferred_imports import diff_pegs, load_pegs

_ROOT = Path(__file__).resolve().parents[2]
_TOML = Path(__file__).with_name("orchestrator_vocabulary.toml")
_LEDGER_MARKER = "# ---- ledger: written by --freeze, never by hand ----\n"
_SELF = frozenset(
    {"tests/conformance/test_orchestrator_vocabulary.py", "tests/conformance/orchestrator_vocabulary.toml"}
)
_UNSCANNED = re.compile(r"^dev/\.test_durations$|\.min\.js$")
_WORD = re.compile(r"(?i)orchestrator")
_IDENTIFIER = re.compile(
    r"(?i:_orchestrator|orchestrator_|t3:orchestrator)|(?<=[a-z0-9])Orchestrator|Orchestrator(?=[A-Z])"
)
_TABLES = ("identifier", "agent_role")
_LEDGER_PREFIX = "shrink_"
_DISPOSITIONS = frozenset({"keep", "move", "retire"})
_FAMILIES = ("word", "identifier", "agent_role", "structural")

type Counts = dict[str, dict[str, int]]


@dataclasses.dataclass(frozen=True)
class Vocabulary:
    class_order: tuple[str, ...]
    dispositions: Mapping[str, str]
    hosts: Mapping[str, tuple[int, ...]]
    rules: tuple[tuple[str, re.Pattern[str]], ...]
    agent_role: re.Pattern[str]
    structural: re.Pattern[str]
    keeps: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, toml_path: Path = _TOML) -> Self:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        classes = data["class"]
        return cls(
            class_order=tuple(classes),
            dispositions={name: spec["disposition"] for name, spec in classes.items()},
            hosts={name: tuple(spec.get("host", ())) for name, spec in classes.items()},
            rules=tuple((rule["class"], re.compile("|".join(rule["paths"]))) for rule in data["rule"]),
            agent_role=re.compile("(?i)" + "|".join(data["phrase"]["agent_role"])),
            structural=re.compile("(?i)" + "|".join(data["phrase"]["structural"])),
            keeps=tuple((keep["path"], keep["text"]) for keep in data.get("keep", ())),
        )

    def classify(self, path: str) -> str | None:
        return next((name for name, pattern in self.rules if pattern.search(path)), None)

    def count(self, path: str, text: str) -> dict[str, int]:
        source = f"{path}\n{text}"
        return {
            "word": len(_WORD.findall(source)),
            "identifier": len(_IDENTIFIER.findall(source)),
            "agent_role": len(self.agent_role.findall(text)),
            "structural": len(self.structural.findall(text)),
        }


def scan(root: Path, vocabulary: Vocabulary, *, minimum_files: int) -> Counts:
    listing = run_checked(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    counts: Counts = {family: {} for family in _FAMILIES}
    scanned = 0
    for path in listing.stdout.split("\0"):
        if not path or path in _SELF or _UNSCANNED.search(path):
            continue
        source_file = root / path
        if not source_file.is_file():
            continue
        raw = source_file.read_bytes()
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for family, count in vocabulary.count(path, text).items():
            if count:
                counts[family][path] = count
    if scanned < minimum_files:
        message = f"scan read {scanned} files; expected at least {minimum_files}"
        raise ValueError(message)
    return counts


def load_ledger(vocabulary: Vocabulary, toml_path: Path = _TOML) -> Counts:
    ledger: Counts = {table: load_pegs(_LEDGER_PREFIX + table, toml_path=toml_path) for table in _TABLES}
    ledger["word"] = {
        path: allowance
        for name in vocabulary.class_order
        for path, allowance in load_pegs(_LEDGER_PREFIX + name, toml_path=toml_path).items()
    }
    return ledger


def misfiled_entries(vocabulary: Vocabulary, toml_path: Path = _TOML) -> list[str]:
    return [
        f"{path}: listed under {name}, its rule says {vocabulary.classify(path)}"
        for name in vocabulary.class_order
        for path in load_pegs(_LEDGER_PREFIX + name, toml_path=toml_path)
        if vocabulary.classify(path) != name
    ]


@dataclasses.dataclass(frozen=True)
class FreezeReport:
    refused: tuple[tuple[str, str], ...]
    unclassified: tuple[str, ...]

    def raise_flags(self) -> list[str]:
        return sorted({f"--raise {path}" for _, path in self.refused})


def _frozen_entries(
    live: Mapping[str, int], old: Mapping[str, int], raised: frozenset[str], *, classified: Mapping[str, bool]
) -> tuple[dict[str, int], list[str]]:
    frozen: dict[str, int] = {}
    refused: list[str] = []
    for path in sorted(set(live) | set(old)):
        if not live.get(path) or not classified.get(path, True):
            continue
        if (path in old and live[path] <= old[path]) or path in raised:
            frozen[path] = live[path]
        else:
            refused.append(path)
            if path in old:
                frozen[path] = old[path]
    return frozen, refused


def _table_text(name: str, entries: Mapping[str, int]) -> str:
    rows = "".join(f"{json.dumps(path, ensure_ascii=False)} = {entries[path]}\n" for path in sorted(entries))
    return f"[{name}]\n{rows}\n" if entries else ""


def _frozen_tables(
    vocabulary: Vocabulary, live: Counts, old: Counts, raised: frozenset[str]
) -> tuple[dict[str, dict[str, int]], list[tuple[str, str]], tuple[str, ...]]:
    tables: dict[str, dict[str, int]] = {name: {} for name in (*vocabulary.class_order, *_TABLES)}
    refused: list[tuple[str, str]] = []
    word_classes = {path: vocabulary.classify(path) for path in set(live["word"]) | set(old["word"])}
    word, word_refused = _frozen_entries(
        live["word"], old["word"], raised, classified={path: name is not None for path, name in word_classes.items()}
    )
    refused.extend(("word", path) for path in word_refused)
    for path, allowance in word.items():
        name = word_classes[path]
        if name is not None:
            tables[name][path] = allowance
    for table in _TABLES:
        tables[table], table_refused = _frozen_entries(live[table], old[table], raised, classified={})
        refused.extend((table, path) for path in table_refused)
    unclassified = tuple(sorted(path for path in live["word"] if word_classes[path] is None))
    return tables, refused, unclassified


def freeze(
    root: Path, vocabulary: Vocabulary, toml_path: Path, *, raised: Sequence[str] = (), minimum_files: int = 2000
) -> FreezeReport:
    head, marker, _ = toml_path.read_text(encoding="utf-8").partition(_LEDGER_MARKER)
    if not marker:
        message = f"{toml_path} has no ledger marker"
        raise ValueError(message)
    live = scan(root, vocabulary, minimum_files=minimum_files)
    tables, refused, unclassified = _frozen_tables(
        vocabulary, live, load_ledger(vocabulary, toml_path), frozenset(raised)
    )
    ledger_text = "".join(_table_text(_LEDGER_PREFIX + name, tables[name]) for name in sorted(tables))
    toml_path.write_text(head + marker + ledger_text.rstrip("\n") + "\n", encoding="utf-8")
    return FreezeReport(refused=tuple(refused), unclassified=unclassified)


def main(argv: Sequence[str], *, root: Path = _ROOT, toml_path: Path = _TOML, minimum_files: int = 2000) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.conformance.test_orchestrator_vocabulary")
    parser.add_argument("--freeze", action="store_true", required=True)
    parser.add_argument("--raise", dest="raised", action="append", default=[], metavar="PATH")
    args = parser.parse_args(argv)
    report = freeze(root, Vocabulary.load(toml_path), toml_path, raised=args.raised, minimum_files=minimum_files)
    if report.unclassified:
        sys.stderr.write(f"no rule classifies these files; add a [[rule]] to {toml_path.name}:\n")
        sys.stderr.writelines(f"{path}\n" for path in report.unclassified)
    if report.refused:
        sys.stderr.write("refused to raise or add; rerun with the files you mean to raise:\n")
        sys.stderr.writelines(f"{flag}\n" for flag in report.raise_flags())
    return 1 if report.refused or report.unclassified else 0


VOCABULARY = Vocabulary.load()

_TINY_TOML = """\
[class.N1]
disposition = "keep"

[class.N3]
disposition = "move"
host = [5021]

[[rule]]
class = "N3"
paths = ['^hooks/']

[[rule]]
class = "N3"
paths = ['^src/special\\.py$']

[[rule]]
class = "N1"
paths = ['^src/', '^docs/']

[phrase]
agent_role = ['the\\s+orchestrator\\s+(?:decides|judges)', 'headless\\s+orchestrator']
structural = ['thin\\s+coordinator']
"""
_TINY_LEDGER = f'{_LEDGER_MARKER}[shrink_N1]\n"src/old.py" = 2\n'


def _repo(tmp_path: Path, files: Mapping[str, str]) -> Path:
    run_checked(["git", "init", "-q", "-b", "main", str(tmp_path)])
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _tiny(tmp_path: Path, ledger: str = _TINY_LEDGER) -> tuple[Vocabulary, Path]:
    toml_path = tmp_path / "vocab.toml"
    toml_path.write_text(_TINY_TOML + ledger, encoding="utf-8")
    return Vocabulary.load(toml_path), toml_path


def _first(over: tuple[tuple[str, int, int], ...]) -> str:
    return f"{len(over)} file(s) over their allowance, first: {over[:10]}"


@pytest.fixture(scope="module")
def tree() -> Counts:
    return scan(_ROOT, VOCABULARY, minimum_files=2000)


def test_a_new_file_naming_the_word_exceeds_the_empty_allowance(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/new.py": "the orchestrator\n"})
    counts = scan(repo, vocabulary, minimum_files=1)
    assert diff_pegs(counts["word"], {}).over_peg == (("src/new.py", 1, 0),)


def test_a_file_over_its_allowance_fails_and_one_under_it_passes(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(
        tmp_path / "r", {"src/old.py": "orchestrator orchestrator orchestrator\n", "src/ok.py": "orchestrator\n"}
    )
    word = scan(repo, vocabulary, minimum_files=2)["word"]
    assert diff_pegs(word, {"src/old.py": 2, "src/ok.py": 5}).over_peg == (("src/old.py", 3, 2),)


def test_the_word_in_a_file_name_is_counted(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/orchestrator_gate.py": ""})
    assert scan(repo, vocabulary, minimum_files=1)["word"] == {"src/orchestrator_gate.py": 1}


def test_the_word_is_matched_in_any_case(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    assert vocabulary.count("src/x.py", "Orchestrator ORCHESTRATOR orchestrators")["word"] == 3


def test_ignored_files_are_not_scanned(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {".gitignore": ".venv/\n", ".venv/lib/pkg.py": "orchestrator\n", "src/seen.py": "x\n"})
    assert scan(repo, vocabulary, minimum_files=1)["word"] == {}


def test_implausibly_small_scan_raises(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {})
    with pytest.raises(ValueError, match="scan read 0 files"):
        scan(repo, vocabulary, minimum_files=1)


def test_failed_git_listing_raises(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent", encoding="utf-8")
    with pytest.raises(CommandFailedError):
        scan(broken, vocabulary, minimum_files=1)


def test_this_census_and_unscannable_files_are_skipped(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(
        tmp_path / "r",
        {
            "tests/conformance/test_orchestrator_vocabulary.py": "orchestrator",
            "tests/conformance/orchestrator_vocabulary.toml": "orchestrator",
            "dev/.test_durations": "orchestrator",
            "static/vendor.min.js": "orchestrator",
            "src/seen.py": "x\n",
        },
    )
    (repo / "src" / "binary.bin").write_bytes(b"orchestrator\0")
    (repo / "src" / "latin1.txt").write_bytes(b"orchestrator \xff")
    assert scan(repo, vocabulary, minimum_files=1)["word"] == {}


def test_a_deleted_tracked_file_is_not_read(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/gone.py": "orchestrator\n"})
    run_checked(["git", "-C", str(repo), "add", "src/gone.py"])
    (repo / "src" / "gone.py").unlink()
    assert scan(repo, vocabulary, minimum_files=0)["word"] == {}


def test_a_file_outside_every_rule_has_no_class(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    assert vocabulary.classify("unlisted/new.py") is None
    assert vocabulary.classify("hooks/new.py") == "N3"
    assert vocabulary.classify("src/new.py") == "N1"


def test_the_first_matching_rule_wins(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    assert vocabulary.classify("src/special.py") == "N3"
    assert vocabulary.classify("src/other.py") == "N1"


@pytest.mark.parametrize(
    "line",
    [
        "the orchestrator decides what ships",
        "The Orchestrator judges the diff",
        "the orchestrator reviews it",
        "the orchestrator\nmerges it",
        "an orchestrator agent",
        "t3:orchestrator chains the phase agents",
        "a headless orchestrator session",
        "the attended orchestrator",
        "the interactive orchestrator",
        "the main orchestrator",
        "the master orchestrator",
    ],
)
def test_a_line_calling_an_agent_or_the_attended_seat_the_orchestrator_is_counted(line: str) -> None:
    assert VOCABULARY.count("src/x.py", line)["agent_role"] == 1


@pytest.mark.parametrize(
    "line",
    [
        "the tick orchestrator schedules the next scan",
        "the orchestrator records the verdict",
        "thin orchestrator over the runner",
        "the orchestrator loop",
        "the orchestrator dispatches the sub-agent",
    ],
)
def test_control_plane_phrases_are_not_agent_role_hits(line: str) -> None:
    assert VOCABULARY.count("src/x.py", line)["agent_role"] == 0


@pytest.mark.parametrize(
    "line", ["Thin coordinator over the engine", "a thin\ncoordinator", "Coordinator for the scan"]
)
def test_structural_senses_are_counted(line: str) -> None:
    assert VOCABULARY.count("src/x.py", line)["structural"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "orchestrator_bash_gate_enabled",
        "handle_enforce_orchestrator_boundary",
        "t3:orchestrator",
        "OrchestratorAnswer",
        "ChainingOrchestrator",
    ],
)
def test_identifier_forms_are_counted(tmp_path: Path, text: str) -> None:
    vocabulary, _ = _tiny(tmp_path)
    assert vocabulary.count("src/x.py", text)["identifier"] == 1


@pytest.mark.parametrize(
    "text",
    ["the orchestrator", "Orchestrator-decides", "the render orchestrators", "tick-orchestrator", "Orchestrator"],
)
def test_prose_forms_are_not_identifier_hits(tmp_path: Path, text: str) -> None:
    vocabulary, _ = _tiny(tmp_path)
    assert vocabulary.count("src/x.py", text)["identifier"] == 0


def test_an_identifier_in_a_file_outside_the_listed_set_fails(tmp_path: Path) -> None:
    vocabulary, _ = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"hooks/listed.py": "orchestrator_gate\n", "src/new.py": "orchestrator_gate\n"})
    identifier = scan(repo, vocabulary, minimum_files=2)["identifier"]
    assert diff_pegs(identifier, {"hooks/listed.py": 1}).over_peg == (("src/new.py", 1, 0),)


def test_freeze_lowers_counts_and_drops_emptied_files(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path, f'{_LEDGER_MARKER}[shrink_N1]\n"src/old.py" = 5\n"src/gone.py" = 1\n')
    repo = _repo(tmp_path / "r", {"src/old.py": "orchestrator\n"})
    report = freeze(repo, vocabulary, toml_path, minimum_files=1)
    assert report.refused == ()
    assert load_pegs("shrink_N1", toml_path=toml_path) == {"src/old.py": 1}


def test_freeze_refuses_to_raise_a_file_it_was_not_told_to(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/old.py": "orchestrator " * 4, "src/new.py": "orchestrator\n"})
    report = freeze(repo, vocabulary, toml_path, minimum_files=2)
    assert {path for _, path in report.refused} == {"src/old.py", "src/new.py"}
    assert load_pegs("shrink_N1", toml_path=toml_path) == {"src/old.py": 2}


def test_freeze_raises_only_the_named_files(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/old.py": "orchestrator " * 4, "src/new.py": "orchestrator\n"})
    report = freeze(repo, vocabulary, toml_path, raised=["src/new.py"], minimum_files=2)
    assert report.refused == (("word", "src/old.py"),)
    assert load_pegs("shrink_N1", toml_path=toml_path) == {"src/new.py": 1, "src/old.py": 2}


def test_freeze_files_each_entry_under_the_class_its_rule_assigns(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"hooks/a.py": "orchestrator\n", "src/b.py": "orchestrator\n"})
    freeze(repo, vocabulary, toml_path, raised=["hooks/a.py", "src/b.py"], minimum_files=2)
    assert (load_pegs("shrink_N3", toml_path=toml_path), load_pegs("shrink_N1", toml_path=toml_path)) == (
        {"hooks/a.py": 1},
        {"src/b.py": 1},
    )
    assert misfiled_entries(vocabulary, toml_path) == []


def test_freeze_refuses_a_file_with_no_class_even_when_named(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"unlisted/new.py": "orchestrator\n"})
    report = freeze(repo, vocabulary, toml_path, raised=["unlisted/new.py"], minimum_files=1)
    assert (report.refused, report.unclassified) == ((), ("unlisted/new.py",))
    assert load_ledger(vocabulary, toml_path)["word"] == {}


def test_freeze_applies_the_same_rule_to_identifier_and_phrase_tables(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path, f'{_LEDGER_MARKER}[shrink_identifier]\n"src/a.py" = 3\n')
    both = "orchestrator_x\nthe orchestrator decides\n"
    repo = _repo(tmp_path / "r", {"src/a.py": "orchestrator_x\n", "src/b.py": both, "src/c.py": both})
    report = freeze(repo, vocabulary, toml_path, raised=["src/b.py"], minimum_files=3)
    assert report.refused == (
        ("word", "src/a.py"),
        ("word", "src/c.py"),
        ("identifier", "src/c.py"),
        ("agent_role", "src/c.py"),
    )
    ledger = load_ledger(vocabulary, toml_path)
    assert (ledger["identifier"], ledger["agent_role"]) == ({"src/a.py": 1, "src/b.py": 1}, {"src/b.py": 1})


def test_freeze_keeps_everything_above_the_ledger_marker_and_is_idempotent(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/old.py": "orchestrator\n"})
    freeze(repo, vocabulary, toml_path, minimum_files=1)
    once = toml_path.read_text(encoding="utf-8")
    assert freeze(repo, vocabulary, toml_path, minimum_files=1).refused == ()
    assert toml_path.read_text(encoding="utf-8") == once
    assert once.startswith(_TINY_TOML + _LEDGER_MARKER)


def test_freeze_writes_paths_sorted_and_quoted(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/b.py": "orchestrator\n", "src/a b.py": "orchestrator\n"})
    freeze(repo, vocabulary, toml_path, raised=["src/b.py", "src/a b.py"], minimum_files=2)
    assert toml_path.read_text(encoding="utf-8").endswith('[shrink_N1]\n"src/a b.py" = 1\n"src/b.py" = 1\n')


def test_freeze_writes_the_ledger_tables_in_sorted_order(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    both = "orchestrator_x\nthe orchestrator decides\n"
    repo = _repo(tmp_path / "r", {"src/a.py": both, "hooks/h.py": both})
    freeze(repo, vocabulary, toml_path, raised=["src/a.py", "hooks/h.py"], minimum_files=2)
    headers = re.findall(r"^\[(shrink_\w+)\]$", toml_path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert headers == ["shrink_N1", "shrink_N3", "shrink_agent_role", "shrink_identifier"]


def test_freeze_needs_the_ledger_marker(tmp_path: Path) -> None:
    vocabulary, toml_path = _tiny(tmp_path)
    toml_path.write_text(_TINY_TOML, encoding="utf-8")
    with pytest.raises(ValueError, match="no ledger marker"):
        freeze(_repo(tmp_path / "r", {"src/a.py": ""}), vocabulary, toml_path, minimum_files=0)


def test_the_command_reports_each_refusal_as_a_ready_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/new.py": "orchestrator\n"})
    assert main(["--freeze"], root=repo, toml_path=toml_path, minimum_files=1) == 1
    assert "--raise src/new.py\n" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--raise", "unlisted/new.py"]])
def test_the_command_asks_for_a_rule_not_a_raise_flag_for_an_unclassified_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    _, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"unlisted/new.py": "orchestrator\n"})
    assert main(["--freeze", *flags], root=repo, toml_path=toml_path, minimum_files=1) == 1
    err = capsys.readouterr().err
    assert "add a [[rule]] to vocab.toml:\nunlisted/new.py\n" in err
    assert "--raise unlisted/new.py" not in err


def test_the_command_succeeds_when_nothing_is_refused(tmp_path: Path) -> None:
    _, toml_path = _tiny(tmp_path)
    repo = _repo(tmp_path / "r", {"src/new.py": "orchestrator\n"})
    assert main(["--freeze", "--raise", "src/new.py"], root=repo, toml_path=toml_path, minimum_files=1) == 0
    assert load_pegs("shrink_N1", toml_path=toml_path) == {"src/new.py": 1}


def test_the_command_does_nothing_without_the_freeze_flag() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_every_file_naming_the_word_has_a_class(tree: Counts) -> None:
    unclassified = sorted(path for path in tree["word"] if VOCABULARY.classify(path) is None)
    assert unclassified == [], f"add a rule in {_TOML.name} for: {unclassified}"


def test_each_ledger_entry_sits_in_the_class_its_rule_assigns() -> None:
    assert misfiled_entries(VOCABULARY) == []


def test_the_word_does_not_grow_in_any_file(tree: Counts) -> None:
    drift = diff_pegs(tree["word"], load_ledger(VOCABULARY)["word"])
    assert drift.over_peg == (), f"reword, do not raise; after a merge of main re-freeze. {_first(drift.over_peg)}"


def test_legacy_identifiers_stay_in_their_listed_files(tree: Counts) -> None:
    drift = diff_pegs(tree["identifier"], load_ledger(VOCABULARY)["identifier"])
    assert drift.over_peg == (), f"an identifier form grew outside its listed files. {_first(drift.over_peg)}"


def test_no_phrase_calls_an_agent_or_the_attended_seat_the_orchestrator(tree: Counts) -> None:
    drift = diff_pegs(tree["agent_role"], load_ledger(VOCABULARY)["agent_role"])
    assert drift.over_peg == (), (
        f"a line calls an agent or the attended seat the orchestrator. {_first(drift.over_peg)}"
    )


def test_the_structural_senses_of_coordinator_have_no_hits(tree: Counts) -> None:
    assert tree["structural"] == {}


def test_every_class_declares_a_disposition_and_every_moved_class_a_host() -> None:
    assert set(VOCABULARY.dispositions.values()) <= _DISPOSITIONS
    assert sorted(name for name, disposition in VOCABULARY.dispositions.items() if disposition != "keep") == sorted(
        name for name, hosts in VOCABULARY.hosts.items() if hosts
    )


def test_each_rule_names_a_declared_class() -> None:
    assert {name for name, _ in VOCABULARY.rules} <= set(VOCABULARY.class_order)


@pytest.mark.parametrize(("path", "sentence"), VOCABULARY.keeps)
def test_a_sentence_kept_on_purpose_is_still_there(path: str, sentence: str) -> None:
    assert sentence in (_ROOT / path).read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
