"""Shared command and origin signals for orchestration boundary hooks."""

import re
import sys
from functools import lru_cache
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.hooks.shell_tokens import HeredocBody, Token

# Alias both identities so a bare ``from orchestration_boundary_signals import
# ...`` (the live hook, whose dir is on sys.path) and the
# ``hooks.scripts.orchestration_boundary_signals`` form (a subprocess/test
# import) resolve the SAME module object.
sys.modules.setdefault("orchestration_boundary_signals", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.orchestration_boundary_signals", sys.modules[__name__])


DISPATCH_TOOLS = frozenset({"Agent", "Task"})


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
    r"(?:^|[;&|\n(){}])[^\S\n]*(?:(?:then|else|elif|do|if|until|while|!)\s+)*"
    r"(?:(?:\d?>{1,2}|\d?<)\s*[^\s<>|;&]+\s+)*(?:\w+=\S+\s+)*"
    r"(?:(?:command(?!\s+-[vV](?:\s|$))|exec|(?:/(?:[\w.-]+/)+)?time|nohup|watch)(?:\s+-\S+)*\s+"
    r"|sudo(?:\s+(?:-u\s+[^-\s]\S*|-\S+))*\s+"
    r"|nice(?:\s+(?:-n\s+[^-\s]\S*|-\S+))*\s+"
    r"|env(?:\s+(?:-u\s+[^-\s=][^\s=]*(?=\s)|-\S+|\w+=\S+))*\s+"
    r"|timeout(?:\s+-\S+)*\s+\d+(?:\.\d+)?[smhd]?\s+"
    r"|xargs(?:\s+(?:-P\s+\d+|-\S+))*\s+)*"
)
RUNNER_PREFIX = (
    r"(?:(?:uvx|npx|yarn)\s+|(?:uv|poetry|pdm|hatch)\s+run\s+"
    r"|pnpm\s+exec\s+|python3?\s+-m\s+)*"
    r"(?:/(?:[\w.-]+/)+)?"
)
PYTEST_VERB_RE = COMMAND_HEAD + RUNNER_PREFIX + r"pytest(?![\w-])"
PYTEST_VERB_FINDER = re.compile(PYTEST_VERB_RE)


@lru_cache(maxsize=1)
def _shell_tokens() -> ModuleType | None:
    try:
        from hooks.scripts.managed_repo import (  # noqa: PLC0415 — keep origin-only hooks cold (#4995)
            teatree_src_on_path,
        )

        with teatree_src_on_path():
            from teatree.hooks import shell_tokens  # noqa: PLC0415 — cold-hook import
    except (ImportError, SyntaxError):
        sys.stderr.write("orchestrator boundary gate: shell lexer unavailable; scanning the raw command.\n")
        return None
    return shell_tokens


_SUBSTITUTION_OPEN_RE = re.compile(r"\$\(|`")
_BARE_PAREN_RE = re.compile(r"""\\.|'[^']*'|"|([()])""", re.DOTALL)
_IN_DOUBLE_RE = re.compile(r"""\\.|"|\$\(""", re.DOTALL)
_PAST_CAP_RE = re.compile(r"""\$\(|[`'"]""")
_MAX_NESTING = 8
_HEREDOC_PIPE_RE = re.compile(r"\|\s*(?:bash|sh|source|python3?)\b|>\(\s*(?:bash|sh)\b")
_FILE_REDIRECT_RE = re.compile(r"(?<!<)>{1,2}\s*(?!\()([^\s<>|;&()]+)")
_TEE_FILE_RE = re.compile(r"(?:^|[;&|])\s*tee(?:\s+-\S+)*\s+([^\s<>|;&()]+)")
_FILE_WRITER_RE = re.compile(r"(?:^|[;&|$(])\s*(?P<cmd>cat|tee)\b[^;&|]*<<")
_EXECUTED_FILE_RE = re.compile(
    r"(?:^|[;&|\n])[^\S\n]*(?:(?:/(?:[\w.-]+/)+)?(?:bash|sh)(?:\s+-\S+)*\s+|source\s+|\.\s+|(?=\./))"
    r"(\.?/?[^\s<>|;&]+)"
)


def _double_quote_end(raw: str, index: int, nesting: int) -> int:
    while match := _IN_DOUBLE_RE.search(raw, index):
        if match[0] == '"':
            return match.end()
        index = match.end()
        if match[0] == "$(":
            index = _substitution_end(raw, match.start(), nesting + 1) + 1
    return len(raw)


def _substitution_end(raw: str, start: int, nesting: int = 0) -> int:
    if nesting > _MAX_NESTING:
        return len(raw)
    depth = 1
    index = start + 2
    while match := _BARE_PAREN_RE.search(raw, index):
        index = match.end()
        if match[0] == '"':
            index = _double_quote_end(raw, index, nesting)
        elif match[1] == "(":
            depth += 1
        elif match[1] == ")":
            depth -= 1
            if not depth:
                return match.start()
    return len(raw)


def _subcommand_text(lexer: ModuleType, raw: str, depth: int, *, heredoc_body: bool = False) -> str:
    live = None if heredoc_body else set(lexer.live_substitution_starts(raw, ("$(", "`")))
    commands: list[str] = []
    resume = 0
    for match in _SUBSTITUTION_OPEN_RE.finditer(raw):
        start = match.start()
        preceding = start - 1
        while preceding >= 0 and raw[preceding] == "\\":
            preceding -= 1
        if start < resume or (start - preceding - 1) % 2 or (live is not None and start not in live):
            continue
        end = raw.find("`", start + 1) if match[0] == "`" else _substitution_end(raw, start)
        if end < 0:
            continue
        resume = end + 1
        commands.append(heavy_command_text(raw[start + len(match[0]) : end], depth + 1))
    return "\n".join(commands)


def _heredoc_executes_body(heredoc: "HeredocBody", command: str, executed_files: dict[str, int]) -> bool:
    line = heredoc.opener_line
    writer = _FILE_WRITER_RE.search(line)
    if _HEREDOC_PIPE_RE.search(line) or writer is None:
        return True
    writer_segment = re.split(r"[;&|]", line[writer.start("cmd") :], maxsplit=1)[0]
    redirect = _FILE_REDIRECT_RE.search(writer_segment) or _TEE_FILE_RE.search(writer_segment)
    if redirect is None:
        return True
    line_start = command.rfind(line, 0, heredoc.end)
    return executed_files.get(redirect.group(1).removeprefix("./"), -1) > line_start + writer.start(
        "cmd"
    ) + redirect.start(1)


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


_QUOTED_PYTEST_SELECTOR_RE = re.compile(r"[^\s;&|(){}]*\.py(?:::[^\s;&|(){}]+)?")
_SHELL_WORDS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})


def _quoted_shell_payload(words: list[str], basenames: set[str]) -> bool:
    if not words:
        return False
    if words[0] in {"eval", "ssh"}:
        return True
    if words[-1] == "<<<" and basenames & _SHELL_WORDS:
        return True
    command_option = words[-2] if words[-1] == "--" and len(words) > 1 else words[-1]
    return bool(re.fullmatch(r"-[a-z]*c", command_option) and (basenames & _SHELL_WORDS or "su" in basenames))


_QUOTED_VERB_RE = re.compile(r"\b(?:make|tox|webpack)\b")
_QUOTED_SEPARATOR_RE = re.compile(r"[;&|\n(){}]")


def _word_text(lexer: ModuleType, tokens: list["Token"], depth: int) -> list[str]:
    result: list[str] = []
    words: list[str] = []
    basenames: set[str] = set()
    after_pytest = False
    for index, token in enumerate(tokens):
        if token.kind is lexer.TokenKind.OP:
            result.append(token.value)
            words.clear()
            basenames.clear()
            after_pytest = False
            continue
        raw = token.raw or token.value
        pipe_to_shell = (
            len(tokens) > index + 2
            and words[:1] == ["echo"]
            and tokens[index + 1].value == "|"
            and tokens[index + 2].value.rsplit("/", 1)[-1] in _SHELL_WORDS
        )
        if _quoted_shell_payload(words, basenames) or pipe_to_shell:
            result.extend(("\n", heavy_command_text(token.value, depth + 1), "\n"))
        elif ("'" not in raw and '"' not in raw) or (
            after_pytest and _QUOTED_PYTEST_SELECTOR_RE.fullmatch(token.value)
        ):
            result.append(token.value)
        else:
            result.append(_QUOTED_SEPARATOR_RE.sub(" ", _QUOTED_VERB_RE.sub(" ", token.value)))
        result.append(" ")
        words.append(token.value)
        basenames.add(token.value.rsplit("/", 1)[-1])
        after_pytest = after_pytest or token.value == "pytest"
    return result


def _heredoc_text(lexer: ModuleType, bodies: list["HeredocBody"], command: str, depth: int) -> list[str]:
    result: list[str] = []
    if not bodies:
        return result
    executed_files = {
        match.group(1).removeprefix("./"): match.start(1) for match in _EXECUTED_FILE_RE.finditer(command)
    }
    for body in bodies:
        if not body.terminated or _heredoc_executes_body(body, command, executed_files):
            executable_text = body.body
            if body.terminated and "python" not in body.opener_line:
                executable_text = heavy_command_text(body.body, depth + 1)
            result.extend(("\n", executable_text, "\n"))
        elif not body.quoted and (substitutions := _subcommand_text(lexer, body.body, depth, heredoc_body=True)):
            result.extend(("\n", substitutions, "\n"))
    return result


@lru_cache(maxsize=4)
def heavy_command_text(command: str, depth: int = 0) -> str:
    lexer = _shell_tokens()
    if lexer is None:
        return command
    if depth > _MAX_NESTING:
        return _PAST_CAP_RE.sub("\n", command)
    from hooks.scripts.mr_cli_fields import strip_heredoc  # noqa: PLC0415 — keep origin-only hooks cold (#4995)

    bodies: list[HeredocBody] = []
    tokens = lexer.tokenize(command, heredoc_bodies=bodies)
    joined = " ".join(strip_heredoc(token.raw) if "<<" in token.raw else token.raw for token in tokens)
    substitutions = _subcommand_text(lexer, joined, depth)
    leading = ["\n", substitutions, "\n"] if substitutions else []
    return "".join([*leading, *_word_text(lexer, tokens, depth), *_heredoc_text(lexer, bodies, command, depth)])
