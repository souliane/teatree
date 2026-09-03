"""Forward-guard: skills reach for an MCP tool before a CLI — 3rd-party or teatree's own.

Two lanes share one ratchet. The 3rd-party lane (``gh`` / ``glab`` /
``sentry-cli``) treats EVERY invocation as a raw call. The ``t3`` lane is
narrower by construction: teatree's own CLI is mostly NOT MCP-served, so only the
``<sub> <verb>`` pairs in :data:`T3_MCP_COVERED` are flagged, and a ``t3``
invocation outside that map is not a finding at all. What the ``t3`` lane asks
for is a DEMOTION, never a deletion — the CLI remains the documented fallback for
a session whose MCP server is not connected, and for a sub-agent that was never
given the server.

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

- Prohibition examples: a line on which a prohibition-specific marker
(``FORBIDDEN``, ``mechanically refused``, ``never run``, ``raw ``gh``, ``not via``…)
governs the command TOKEN — the marker and the command sit on the SAME line.
Rewriting these to call the MCP would invert their meaning, so they are not
migration targets and must never be ledgered — a new prohibition example lands
freely. The marker must occur on the command's own line: a ubiquitous word
(``never``, ``don't``, ``instead of``) two lines away does NOT suppress a real
call, and generic phrasings that pervade unrelated prose are deliberately not
markers — only phrasing that actually governs a command counts.
- Per-line allow pragma: a line carrying ``mcp-ratchet: allow`` (the escape
hatch for a ratified CLI exception the classifier can't infer).

Detection is textual and self-contained (stdlib + ``tomllib`` only —
``teatree.quality`` declares no internal tach dependency this module needs): for
each ``gh`` / ``glab`` / ``sentry-cli`` command keyword on a line, the first one
or two bare sub-command words (flags, ``<placeholders>`` and ``owner/repo`` paths
skipped) form a stable SIGNATURE. A ``t3`` keyword instead yields one signature
per adjacent bare-word pair found in :data:`T3_MCP_COVERED`, so the overlay word
— present in ``t3 <overlay> ticket merge``, absent when an invocation omits it —
never shifts the match. The ledger key is ``<relpath>::<signature>`` — stable
across line moves and low-churn: many occurrences of one signature in one file
collapse to a single entry.

The lane is root- AND ledger-parameterised end to end: ``--root`` picks the tree
whose ``skills/`` directory is scanned, ``--ledger`` names the file to check or
rewrite. A repo that VENDORS this module can therefore guard a second skills tree
against its own ledger without either side writing the other's file, and the
ledger a rebaseline writes carries the exact flags that reproduce it.
"""

import argparse
import dataclasses
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar

ALLOW_PRAGMA = "mcp-ratchet: allow"
_MODULE_COMMAND = "python -m teatree.quality.skill_cli_ratchet"
_SKILLS_DIR = "skills"
_COMMANDS: frozenset[str] = frozenset({"gh", "glab", "sentry-cli"})
_MAX_SUBCOMMANDS = 2

#: ``<sub> <verb>`` pairs of teatree's OWN ``t3`` CLI that an MCP tool already
#: serves -> the tool that serves them. A blanket ``t3`` ban would be wrong: most
#: of the CLI (worktree/workspace provisioning, the run/e2e/db lanes, doctor,
#: setup, the loop verbs, issuing a merge CLEAR) has no MCP twin and must stay a
#: shell-out, so ONLY these pairs are flagged. Keys are overlay-free — the pair is
#: matched across adjacent word pairs, so ``t3 <overlay> ticket merge``,
#: ``t3 teatree ticket merge`` and the overlay-less form all reduce to one key
#: without this module knowing an installation's overlay names.
T3_MCP_COVERED: Mapping[str, str] = {
    "config_setting get": "config_setting_get",
    "config_setting set": "config_setting_set",
    "lifecycle record-e2e-run": "record_e2e_run",
    "lifecycle visit-phase": "ticket_visit_phase",
    "notify send": "notify_user",
    "pr create": "pr_create",
    "questions answer": "question_answer",
    "questions list": "question_list",
    "review post-comment": "review_post_comment",
    "review post-draft-note": "review_post_draft_note",
    "review-request check": "review_request_check",
    "review-request post": "review_request_post",
    "slack react": "slack_react",
    "tasks create": "task_create",
    "tasks list": "task_list",
    "ticket list": "ticket_list",
    "ticket merge": "pr_merge",
    "workspace teardown": "worktree_teardown",
    "worktree status": "worktree_status",
    "worktree teardown": "worktree_teardown",
}
_T3_COMMAND = "t3"
_T3_MAX_WORDS = 6

# How far ABOVE a `t3` command to look for its MCP tool name. Inside a fence the
# lookback is measured from the FENCE OPENER, not the command line, because one
# MCP-first sentence above a block governs every command in it.
_MIGRATION_LOOKBACK = 5

# A marker suppresses ONLY when it shares the command token's line (no window):
# every entry is phrasing that governs a command, not a ubiquitous prose word.
# ``never `` (never immediately before a backticked command) is the "do X, never
# Y" forbidden-command-list idiom; bare ``never``/``don't``/``do not``/``instead
# of``/``not available`` are deliberately absent — they pervade unrelated prose.
_PROHIBITION_MARKERS: tuple[str, ...] = (
    "forbidden",
    "mechanically refused",
    "mechanically blocked",
    "out of scope",
    "not authorized",
    "raw `gh",
    "raw `glab",
    "never `",
    "never use",
    "never call",
    "never run",
    "never raw",
    "never directly",
    "never assign",
    "never reach for",
    "never by querying",
    "not via",
    "do not call",
    "must not run",
)

# ``gh``/``glab``/``sentry-cli`` as a whole token, optionally reached through a
# path (``/usr/bin/gh``, ``./gh``) or a prefix word (``command gh``).
_COMMAND_RE = re.compile(r"(?<![\w-])(gh|glab|sentry-cli)(?![\w-])")
_T3_RE = re.compile(r"(?<![\w-])t3(?![\w-])")
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
        if (tool := T3_MCP_COVERED.get(self.signature.removeprefix(f"{_T3_COMMAND} "))) is not None:
            return (
                f"{self.path}:{self.line_no}: `{self.signature}` — the teatree MCP already serves this: name "
                f"`mcp__teatree__{tool}` first. The CLI stays the documented fallback (MCP server not "
                f"connected, or a sub-agent that was never given the server), so demote it rather than delete "
                f"it — then add its `{self.key}` line to the ledger, or mark the line `{ALLOW_PRAGMA}`."
            )
        return (
            f"{self.path}:{self.line_no}: raw `{self.signature}` call — route it through the teatree MCP "
            f"forge tools (e.g. mcp__teatree__<forge>_issue / _pr_get / _issue_search). If no MCP tool covers "
            f"it yet, keep the CLI fallback and add its `{self.key}` line to the ledger, or mark the line "
            f"`{ALLOW_PRAGMA}`."
        )


def _signature(command: str, rest: str) -> str | None:
    head = rest.split()
    if head and head[0].strip(_STRIP_CHARS).lower() == "cli":
        return None  # "the gh CLI" — a prose reference to the tool, not an invocation
    words: list[str] = []
    for raw in head[:_SKIP_AHEAD]:
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


def _bare_words(rest: str, limit: int) -> list[str]:
    words: list[str] = []
    for raw in rest.split()[:limit]:
        token = raw.strip(_STRIP_CHARS)
        if not token or token.startswith("-") or "/" in token or "<" in token or "$" in token:
            continue
        if _BARE_WORD_RE.match(token) or "_" in token:
            words.append(token)
    return words


def t3_signatures_in_fragment(fragment: str) -> list[str]:
    """Every ``t3 <sub> <verb>`` signature in *fragment* that an MCP tool serves.

    Adjacent pairs are tested rather than a fixed position, so the overlay word an
    invocation may or may not carry never shifts the match.
    """
    found: list[str] = []
    for match in _T3_RE.finditer(fragment):
        words = _bare_words(fragment[match.end() :], _T3_MAX_WORDS)
        found.extend(
            f"{_T3_COMMAND} {pair}"
            for first, second in pairwise(words)
            if (pair := f"{first} {second}") in T3_MCP_COVERED
        )
    return found


def is_already_migrated(context: str, signature: str) -> bool:
    """Does *context* already name the MCP tool that serves *signature*?

    An MCP-first site that KEEPS its CLI fallback is the target state, not debt —
    suppressing it is what lets the ledger mean "not yet migrated" and lets the
    floor genuinely shrink as sites are converted.

    *context* is the command's own line plus :data:`_MIGRATION_LOOKBACK` lines
    above it, because the house style puts the MCP sentence in the PROSE ABOVE a
    fenced block (an ```mcp__teatree__…``` name is not shell, so it cannot go
    inside the fence). A same-line-only test would therefore read every correctly
    migrated fenced site as un-migrated. The lookback is safe here in a way it is
    NOT for :func:`is_prohibition`: the marker is one exact, unambiguous token
    (``mcp__teatree__<tool>``) rather than a prose word that pervades unrelated text.
    """
    tool = T3_MCP_COVERED.get(signature.removeprefix(f"{_T3_COMMAND} "))
    return tool is not None and f"mcp__teatree__{tool}" in context


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


def is_prohibition(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _PROHIBITION_MARKERS)


def raw_calls_in(source: str, path: str) -> list[RawCall]:
    lines = source.splitlines()
    calls: list[RawCall] = []
    in_fence = False
    fence_start = 0
    for idx, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            fence_start = idx if in_fence else fence_start
            continue
        if ALLOW_PRAGMA in line:
            continue
        if in_fence and line.lstrip().startswith("#"):
            continue
        fragments = code_fragments(line, in_fence=in_fence)
        signatures = [
            sig
            for fragment in fragments
            for sig in (*signatures_in_fragment(fragment), *t3_signatures_in_fragment(fragment))
        ]
        if not signatures or is_prohibition(line):
            continue
        anchor = fence_start if in_fence else idx
        context = "\n".join(lines[max(0, anchor - _MIGRATION_LOOKBACK) : idx + 1])
        calls.extend(
            RawCall(path=path, signature=sig, line_no=idx + 1, text=line.strip())
            for sig in signatures
            if not is_already_migrated(context, sig)
        )
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
        "# Grandfathered CLI calls in skills -- the skill-cli-ratchet ledger.\n"
        "# Each line is `<skill file>::<command signature>` for a call the skills still make from a CLI an\n"
        "# MCP tool could serve: a raw 3rd-party call (gh/glab/sentry-cli), or a `t3 <sub> <verb>` pair the\n"
        "# teatree MCP already covers. A documented CLI fallback lane, a bootstrap exception, or a call site\n"
        "# with no MCP tool yet all belong here rather than being migrated.\n"
        "# MCP-not-connected fallbacks -- e.g. `gh issue list` / `gh pr list` when the MCP server is down --\n"
        "# are intentional documented fallback lanes: ledgered here rather than migrated.\n"
        "# The gate is RED on any LIVE raw call NOT listed here (a NEW shell-out, named) and RED on any listed\n"
        "# key that no longer occurs (forced banking -- remove it once migrated). Per-item, set-union mergeable.\n"
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
    def write(
        cls, ledger: Path, keys: Iterable[str], *, regenerate_command: str = f"{_MODULE_COMMAND} --update-baseline"
    ) -> None:
        body = "".join(f"{key}\n" for key in sorted(set(keys)))
        regenerate = f"# Regenerate the exact live set with: {regenerate_command}\n"
        ledger.write_text(cls._HEADER + regenerate + body, encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class RatchetConfig:
    __test__: ClassVar[bool] = False

    grandfathered: frozenset[str] = frozenset()


def load_config(pyproject: Path) -> RatchetConfig:
    ledger = Ledger.path_for(pyproject)
    grandfathered = Ledger.load(ledger) if ledger is not None else frozenset()
    return RatchetConfig(grandfathered=grandfathered)


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


def _regenerate_command(*, root: Path | None, ledger: Path | None) -> str:
    """The exact invocation that reproduces the ledger about to be written."""
    parts = [_MODULE_COMMAND, "--update-baseline"]
    if root is not None:
        parts += ["--root", root.as_posix()]
    if ledger is not None:
        parts += ["--ledger", ledger.as_posix()]
    return " ".join(parts)


def update_baseline(root: Path | None = None, ledger: Path | None = None) -> int:
    scanned = root if root is not None else _repo_root()
    target = ledger if ledger is not None else Ledger.path_for(scanned / "pyproject.toml")
    if target is None:
        sys.stdout.write("no [tool.teatree.skill_cli_ratchet] baseline_file configured and no --ledger given\n")
        return 1
    keys = {call.key for call in find_raw_calls(scanned)}
    Ledger.write(target, keys, regenerate_command=_regenerate_command(root=root, ledger=ledger))
    sys.stdout.write(f"wrote {len(keys)} grandfathered raw-call keys to {target}\n")
    return 0


def _config_for(*, root: Path, ledger: Path | None) -> RatchetConfig:
    if ledger is None:
        return load_config(root / "pyproject.toml")
    return RatchetConfig(grandfathered=Ledger.load(ledger))


def run(root: Path | None = None, ledger: Path | None = None) -> int:
    scanned = root if root is not None else _repo_root()
    report = build_report(root=scanned, config=_config_for(root=scanned, ledger=ledger))
    if not report.failed:
        sys.stdout.write(f"skill-cli-ratchet: OK ({len(report.live_keys)} grandfathered raw calls)\n")
        return 0
    for line in [*report.summary_lines(), *report.stale_lines()]:
        sys.stdout.write(line + "\n")
    return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=_MODULE_COMMAND, description="check or rebaseline a skills tree")
    parser.add_argument(
        "--update-baseline", action="store_true", help="rewrite the ledger from the live set instead of checking it"
    )
    parser.add_argument("--root", type=Path, help="tree whose `skills/` directory is scanned (default: this repo)")
    parser.add_argument(
        "--ledger", type=Path, help="ledger to check or rewrite (default: baseline_file under --root's pyproject.toml)"
    )
    return parser.parse_args(argv)


def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.update_baseline:
        return update_baseline(args.root, args.ledger)
    return run(args.root, args.ledger)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
