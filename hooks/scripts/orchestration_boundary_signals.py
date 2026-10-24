"""Shared command and origin signals for orchestration boundary hooks."""

import re
import sys
from functools import lru_cache

from hooks.scripts.mr_cli_fields import strip_heredoc
from teatree.hooks.shell_tokens import HeredocBody, TokenKind, raw_substitution_sees_live, tokenize

# Alias both identities so a bare ``from orchestration_boundary_signals import
# ...`` (the live hook, whose dir is on sys.path) and the
# ``hooks.scripts.orchestration_boundary_signals`` form (a subprocess/test
# import) resolve the SAME module object.
sys.modules.setdefault("orchestration_boundary_signals", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.orchestration_boundary_signals", sys.modules[__name__])


def call_is_from_subagent(data: dict) -> bool:
    """True when the gated tool call originates from a sub-agent.

    Main-vs-sub-agent signal (#115 root cause). The PreToolUse payload's
    ``transcript_path`` ALWAYS points at the PARENT session transcript, even for
    a sub-agent's tool call (a sub-agent's own turns live in a separate
    ``…/subagents/agent-<id>.jsonl`` the hook never receives), and the parent
    transcript's tail entries carry ``isSidechain: false`` — so a
    transcript-``isSidechain`` read MISDETECTS every genuine sub-agent as the
    main agent. The reliable signal is on the payload itself: a sub-agent call
    carries a non-empty ``agent_id`` (and ``agent_type``); a main-agent call
    omits it. Empty/absent ``agent_id`` ⇒ main agent.
    """
    return bool(data.get("agent_id"))


COMMAND_HEAD = (
    r"(?:^|[;&|\n(){}])\s*(?:(?:then|do)\s+)?(?:\w+=\S+\s+)*"
    r"(?:(?:(?:command(?!\s+-[vV](?:\s|$))|exec|time|nice|sudo|nohup)(?:\s+-\S+)*"
    r"|env(?:\s+(?:-\S+|\w+=\S+))*)\s+)*"
)
RUNNER_PREFIX = (
    r"(?:(?:uvx|npx|xargs)\s+|(?:uv|poetry|pdm|hatch)\s+run\s+"
    r"|python3?\s+-m\s+|timeout\s+\d+\s+)*"
)
PYTEST_VERB_RE = COMMAND_HEAD + RUNNER_PREFIX + r"pytest(?![\w-])"
PYTEST_VERB_FINDER = re.compile(PYTEST_VERB_RE)


_SUBCOMMAND_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`", re.DOTALL)
_HEREDOC_INTERPRETER_RE = re.compile(r"(?:^|[;&|])\s*(?:bash|sh|python3?)\b[^;&|\n]*<<")
_HEREDOC_PIPE_RE = re.compile(r"\|\s*(?:bash|sh)\b|>\(\s*(?:bash|sh)\b")
_FILE_REDIRECT_RE = re.compile(r"(?<!<)>{1,2}\s*([^\s<>|;&]+)")


def _subcommand_text(raw: str, *, heredoc_body: bool = False) -> str:
    commands: list[str] = []
    for match in _SUBCOMMAND_RE.finditer(raw):
        opener = "$(" if match.group(1) is not None else "`"
        preceding = match.start() - 1
        while preceding >= 0 and raw[preceding] == "\\":
            preceding -= 1
        if (match.start() - preceding - 1) % 2:
            continue
        if heredoc_body or raw_substitution_sees_live(raw, (opener,), report_from=match.start()):
            commands.append(heavy_command_text(match.group(1) or match.group(2) or ""))
    return "\n".join(commands)


def _heredoc_executes_body(heredoc: HeredocBody, command: str) -> bool:
    line = heredoc.opener_line
    if _HEREDOC_INTERPRETER_RE.search(line) or _HEREDOC_PIPE_RE.search(line):
        return True
    redirect = _FILE_REDIRECT_RE.search(line)
    if redirect is None:
        return False
    executed_file = r"(?:^|[;&|\n])\s*(?:bash|sh)\s+" + re.escape(redirect.group(1)) + r"(?:\s|$)"
    return bool(re.search(executed_file, command[heredoc.end :]))


_PYTEST_TARGETED_RE = re.compile(r"(?:^|\s)(?:-k|--deselect)(?:[=\s]|$)|::|(?:^|\s)\S*\.py(?:::|\s|$)")


def pytest_command_is_targeted(command: str) -> bool:
    matches = list(PYTEST_VERB_FINDER.finditer(command))
    if not matches:
        return False
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(command)
        segment = command[match.end() : end]
        boundary = re.search(r"[;&|\n(){}]", segment)
        if boundary is not None:
            segment = segment[: boundary.start()]
        if not _PYTEST_TARGETED_RE.search(segment):
            return False
    return True


def command_matches_non_pytest_heavy(command: str, heavy_pattern: re.Pattern[str]) -> bool:
    return bool(heavy_pattern.search(PYTEST_VERB_FINDER.sub(" __pytest__ ", command)))


_HELP_OR_VERSION_RE = re.compile(r"(?:^|\s)(?:--help|-h|--version)(?=\s|$)")
_NEVER_HELP_EXEMPT_RE = re.compile(r"\bfind\s+\S+.*-exec\b|\bls\s+-[a-zA-Z]*R[a-zA-Z]*\b")


def heavy_command_is_help_only(command: str, heavy_pattern: re.Pattern[str]) -> bool:
    saw_heavy = False
    for segment in re.split(r"[;&|\n(){}]", command):
        if heavy_pattern.search(segment):
            saw_heavy = True
            if _NEVER_HELP_EXEMPT_RE.search(segment) or not _HELP_OR_VERSION_RE.search(segment):
                return False
    return saw_heavy


@lru_cache(maxsize=4)
def heavy_command_text(command: str) -> str:
    bodies: list[HeredocBody] = []
    tokens = tokenize(command, heredoc_bodies=bodies)
    result: list[str] = []
    subcommands = _subcommand_text(strip_heredoc(" ".join(token.raw for token in tokens)))
    if subcommands:
        result.extend(("\n", subcommands, "\n"))
    words: list[str] = []
    for token in tokens:
        if token.kind is TokenKind.OP:
            result.append(token.value)
            words.clear()
            continue
        raw = token.raw or token.value
        shell_script_arg = len(words) > 1 and words[-2] in {"bash", "sh"} and bool(re.fullmatch(r"-[a-z]*c", words[-1]))
        if shell_script_arg:
            result.extend(("\n", heavy_command_text(token.value), "\n"))
        elif "'" not in raw and '"' not in raw:
            result.append(token.value)
        else:
            result.append(" ")
        result.append(" ")
        words.append(token.value)
    for body in bodies:
        if not body.terminated or _heredoc_executes_body(body, command):
            executable_text = body.body
            if body.terminated and "python" not in body.opener_line:
                executable_text = heavy_command_text(body.body)
            result.extend(("\n", executable_text, "\n"))
        elif not body.quoted:
            result.extend(("\n", _subcommand_text(body.body, heredoc_body=True), "\n"))
    return "".join(result)
