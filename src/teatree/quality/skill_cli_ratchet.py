"""Forward-guard: skills stop shelling out to 3rd-party CLIs (``gh``/``glab``/``sentry-cli``).

Issue #35 (umbrella #3076) routes every skill's forge/monitoring reach through
the teatree MCP tools instead of raw ``gh`` / ``glab`` / ``sentry-cli`` calls.
A wholesale migration cannot land in one step — several call sites have no MCP
equivalent yet and legitimately keep a CLI fallback, and the
``skills/platforms/references/`` recipe library is a deliberately-documented CLI
fallback lane. So this gate is a **ratchet**, not a ban: it grandfathers the
current raw-call surface as an explicit per-item LEDGER and turns RED only when a
NEW raw call appears (a skill regressing to a shell-out) or a grandfathered entry
no longer exists (forced banking — the migrated entry must be removed so the
floor can only shrink). Unlike a scalar count, a per-key list merges as a git
set-union, so two disjoint skill PRs never collide.

What is NOT a raw call (excluded from the ledger, so a reviewer never has to
touch it):

- Prohibition examples: lines whose surrounding context forbids the command
("never", "FORBIDDEN", "mechanically refused", "raw ``gh pr merge``"). Rewriting
these to call the MCP would invert their meaning, so they are not migration
targets and must never be ledgered — a new prohibition example lands freely.
- Per-line allow pragma: a line carrying ``mcp-ratchet: allow`` (the escape
hatch for a ratified CLI exception the classifier can't infer).

Detection is textual and self-contained (stdlib + ``tomllib`` only —
``teatree.quality`` declares no internal tach dependency this module needs): for
each ``gh`` / ``glab`` / ``sentry-cli`` command keyword on a line, the first one
or two bare sub-command words (flags, ``<placeholders>`` and ``owner/repo`` paths
skipped) form a stable SIGNATURE. The ledger key is ``<relpath>::<signature>`` —
stable across line moves and low-churn: many occurrences of one signature in one
file collapse to a single entry.
"""

import dataclasses
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar

ALLOW_PRAGMA = "mcp-ratchet: allow"
_SKILLS_DIR = "skills"
_COMMANDS: frozenset[str] = frozenset({"gh", "glab", "sentry-cli"})
_MAX_SUBCOMMANDS = 2
_PROHIBITION_WINDOW = 2

_PROHIBITION_MARKERS: tuple[str, ...] = (
    "forbidden",
    "never",
    "not raw",
    "mechanically",
    "refused",
    "prohibited",
    "do not",
    "don't",
    "instead of",
    "out of scope",
    "not valid",
    "rejects",
    "is blocked",
    "are blocked",
    "unavailable",
    "isn't available",
    "not available",
)

_COMMAND_RE = re.compile(r"(?<![\w./-])(gh|glab|sentry-cli)(?![\w-])")
_BARE_WORD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_FENCE_RE = re.compile(r"^\s*```")
_STRIP_CHARS = "`'\",.();:"
_SKIP_AHEAD = 6


@dataclasses.dataclass(frozen=True)
class RawCall:
    path: str
    signature: str
    line_no: int
    text: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.signature}"

    @property
    def message(self) -> str:
        return (
            f"{self.path}:{self.line_no}: raw `{self.signature}` call — route it through the teatree MCP "
            f"forge tools (e.g. mcp__teatree__<forge>_issue / _pr_get / _issue_search). If no MCP tool covers "
            f"it yet, keep the CLI fallback and add its `{self.key}` line to the ledger, or mark the line "
            f"`{ALLOW_PRAGMA}`."
        )


def _signature(command: str, rest: str) -> str | None:
    words: list[str] = []
    for raw in rest.split()[:_SKIP_AHEAD]:
        token = raw.strip(_STRIP_CHARS)
        if not token or token.startswith("-") or "/" in token or "<" in token or "$" in token:
            continue
        if token in _COMMANDS:
            continue
        if _BARE_WORD_RE.match(token):
            words.append(token)
            if len(words) == _MAX_SUBCOMMANDS:
                break
    if not words:
        return None
    return " ".join([command, *words])


def code_fragments(line: str, *, in_fence: bool) -> list[str]:
    if in_fence:
        return [line]
    return _INLINE_CODE_RE.findall(line)


def signatures_in_fragment(fragment: str) -> list[str]:
    found: list[str] = []
    for match in _COMMAND_RE.finditer(fragment):
        signature = _signature(match.group(1), fragment[match.end() :])
        if signature is not None:
            found.append(signature)
    return found


def is_prohibition(lines: list[str], idx: int) -> bool:
    lo = max(0, idx - _PROHIBITION_WINDOW)
    hi = min(len(lines), idx + _PROHIBITION_WINDOW + 1)
    window = " ".join(lines[lo:hi]).lower()
    return any(marker in window for marker in _PROHIBITION_MARKERS)


def raw_calls_in(source: str, path: str) -> list[RawCall]:
    lines = source.splitlines()
    calls: list[RawCall] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if ALLOW_PRAGMA in line:
            continue
        if in_fence and line.lstrip().startswith("#"):
            continue
        fragments = code_fragments(line, in_fence=in_fence)
        signatures = [sig for fragment in fragments for sig in signatures_in_fragment(fragment)]
        if not signatures or is_prohibition(lines, idx):
            continue
        calls.extend(RawCall(path=path, signature=sig, line_no=idx + 1, text=line.strip()) for sig in signatures)
    return calls


def collect_skill_files(root: Path) -> list[Path]:
    skills = root / _SKILLS_DIR
    if not skills.is_dir():
        return []
    return sorted(p for p in skills.rglob("*.md") if p.is_file())


def find_raw_calls(root: Path) -> list[RawCall]:
    calls: list[RawCall] = []
    for path in collect_skill_files(root):
        rel = path.relative_to(root).as_posix()
        calls.extend(raw_calls_in(_read(path), rel))
    return calls


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclasses.dataclass(frozen=True)
class RatchetReport:
    __test__: ClassVar[bool] = False

    raw_calls: tuple[RawCall, ...]
    grandfathered: frozenset[str]

    @property
    def live_keys(self) -> frozenset[str]:
        return frozenset(call.key for call in self.raw_calls)

    @property
    def unknown_calls(self) -> tuple[RawCall, ...]:
        seen: set[str] = set()
        out: list[RawCall] = []
        for call in self.raw_calls:
            if call.key in self.grandfathered or call.key in seen:
                continue
            seen.add(call.key)
            out.append(call)
        return tuple(out)

    @property
    def stale_entries(self) -> tuple[str, ...]:
        return tuple(sorted(self.grandfathered - self.live_keys))

    @property
    def failed(self) -> bool:
        return bool(self.unknown_calls or self.stale_entries)

    def summary_lines(self) -> list[str]:
        return [f"  - {call.message}" for call in self.unknown_calls]

    def stale_lines(self) -> list[str]:
        return [f"  - {key} (no longer a raw call — remove it from the ledger)" for key in self.stale_entries]


class Ledger:
    _HEADER: ClassVar[str] = (
        "# Grandfathered raw gh/glab/sentry-cli calls in skills -- the skill-cli-ratchet ledger.\n"
        "# Each line is `<skill file>::<command signature>` for a raw 3rd-party-CLI call the skills still\n"
        "# make (a documented CLI fallback lane, a bootstrap exception, or a call site with no MCP tool yet).\n"
        "# The gate is RED on any LIVE raw call NOT listed here (a NEW shell-out, named) and RED on any listed\n"
        "# key that no longer occurs (forced banking -- remove it once migrated). Per-item, set-union mergeable.\n"
        "# Regenerate the exact live set with: python -m teatree.quality.skill_cli_ratchet --update-baseline\n"
    )

    @staticmethod
    def path_for(pyproject: Path) -> Path | None:
        raw = _read_table(pyproject)
        if "baseline_file" not in raw:
            return None
        return pyproject.parent / str(raw["baseline_file"])

    @staticmethod
    def load(ledger: Path) -> frozenset[str]:
        if not ledger.is_file():
            return frozenset()
        lines = ledger.read_text(encoding="utf-8").splitlines()
        return frozenset(stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#"))

    @classmethod
    def write(cls, ledger: Path, keys: Iterable[str]) -> None:
        body = "".join(f"{key}\n" for key in sorted(set(keys)))
        ledger.write_text(cls._HEADER + body, encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class RatchetConfig:
    __test__: ClassVar[bool] = False

    mode: str = "warn"
    grandfathered: frozenset[str] = frozenset()


def load_config(pyproject: Path) -> RatchetConfig:
    raw = _read_table(pyproject)
    mode = str(raw["mode"]) if "mode" in raw else "warn"
    ledger = Ledger.path_for(pyproject)
    grandfathered = Ledger.load(ledger) if ledger is not None else frozenset()
    return RatchetConfig(mode=mode, grandfathered=grandfathered)


def _read_table(pyproject: Path) -> Mapping[str, Any]:
    if not pyproject.is_file():
        return {}
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    teatree = tool.get("teatree", {}) if isinstance(tool, dict) else {}
    table = teatree.get("skill_cli_ratchet", {}) if isinstance(teatree, dict) else {}
    return table if isinstance(table, dict) else {}


def build_report(*, root: Path, config: RatchetConfig) -> RatchetReport:
    return RatchetReport(raw_calls=tuple(find_raw_calls(root)), grandfathered=config.grandfathered)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def update_baseline(root: Path) -> int:
    ledger = Ledger.path_for(root / "pyproject.toml")
    if ledger is None:
        sys.stdout.write("no [tool.teatree.skill_cli_ratchet] baseline_file configured\n")
        return 1
    keys = {call.key for call in find_raw_calls(root)}
    Ledger.write(ledger, keys)
    sys.stdout.write(f"wrote {len(keys)} grandfathered raw-call keys to {ledger}\n")
    return 0


def run(root: Path) -> int:
    report = build_report(root=root, config=load_config(root / "pyproject.toml"))
    if not report.failed:
        sys.stdout.write(f"skill-cli-ratchet: OK ({len(report.live_keys)} grandfathered raw calls)\n")
        return 0
    for line in [*report.summary_lines(), *report.stale_lines()]:
        sys.stdout.write(line + "\n")
    return 1


def _main(argv: list[str]) -> int:
    root = _repo_root()
    if argv and argv[0] == "--update-baseline":
        return update_baseline(root)
    return run(root)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
