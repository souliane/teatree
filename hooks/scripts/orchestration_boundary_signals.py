"""Shared leaf: orchestration-boundary command/origin signals (#115, #1442).

Two primitives that the orchestrator-boundary gate family reads to decide
whether a PreToolUse call should be governed:

* :func:`call_is_from_subagent` — main-agent vs sub-agent origin.
* :data:`PYTEST_VERB_RE` / :data:`PYTEST_VERB_FINDER` — a foreground
    test-run command shape.

They were inline in ``hook_router`` and are now extracted so BOTH the heavy-Bash
deny gate (``handle_enforce_orchestrator_boundary``) and the broader
orchestrator-investigation WARN nudge (``orchestrator_investigation_gate``) read
them from one shared leaf — neither gate family imports a private off the other,
and ``hook_router`` nets smaller. This is a dependency-free leaf: it imports
nothing first-party, so ``hook_router`` and the gate leaves import IT without a
cycle.
"""

import re
import sys

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


# ``pytest`` must match only in a VERB POSITION — never inside a quoted arg, a
# branch name, a ``-m``/``--title`` message, or a hyphenated package name
# (``pytest-django``). A bare ``\bpytest\b`` mis-matched a ``git commit -m 'fix
# pytest fixture'`` / ``git branch x-pytest`` / ``uv add pytest-django`` (#1178).
# So anchor it to a command head: start-of-string OR a shell separator (``;``
# ``&&`` ``||`` ``|`` newline ``(`` ``{``), then optional env-var assignments,
# optional (possibly-stacked) command-wrapper prefixes
# (``command``/``exec``/``time``/``nice``), and an optional Python runner prefix
# — note ``uvx`` runs a tool DIRECTLY with no ``run`` (``uvx pytest``), while
# ``uv``/``poetry``/``pdm``/``hatch`` DO need ``run``, and ``python[3] -m`` —
# then ``pytest`` NOT followed by a word char or hyphen. The separator branch
# keeps the shell-grammar bypass guard intact (``git status && pytest`` still
# matches); the trailing ``(?![\w-])`` keeps the match pinned to ``pytest`` so
# wrapper prefixes never widen to other tools (``uvx ruff`` stays unmatched).
COMMAND_HEAD = (
    r"(?:^|[;&|\n(){}])\s*(?:\w+=\S+\s+)*"
    r"(?:(?:(?:command|exec|time|nice|sudo|nohup)(?:\s+-\S+)*"
    r"|env(?:\s+(?:-\S+|\w+=\S+))*)\s+)*"
)
PYTEST_VERB_RE = COMMAND_HEAD + (
    r"(?:uvx\s+|(?:uv|poetry|pdm|hatch)\s+run\s+|python3?\s+-m\s+)?"
    r"pytest(?![\w-])"
)
PYTEST_VERB_FINDER = re.compile(PYTEST_VERB_RE)


_QUOTED_SHELL_TEXT_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


def _executable_backticks(text: str) -> str:
    result: list[str] = []
    escaped = False
    for char in text:
        result.append("\n" if char == "`" and not escaped else char)
        escaped = char == "\\" and not escaped
    return "".join(result)


def _quoted_shell_text(match: re.Match[str]) -> str:
    text = match.group()
    if re.search(r"(?:^|[;&|\n])\s*(?:bash|sh)\s+-c\s*$", match.string[: match.start()]):
        return "\n" + _executable_backticks(text[1:-1]) + "\n"
    if text.startswith("'") or ("$(" not in text and "`" not in text):
        return re.sub(r"[;&|\n(){}]", " ", text)
    return _executable_backticks(text)


def command_with_executable_separators(command: str) -> str:
    if not any(quote in command for quote in ("'", '"', "`")):
        return command
    result: list[str] = []
    last = 0
    for match in _QUOTED_SHELL_TEXT_RE.finditer(command):
        result.extend((_executable_backticks(command[last : match.start()]), _quoted_shell_text(match)))
        last = match.end()
    result.append(_executable_backticks(command[last:]))
    return "".join(result)


_PYTEST_TARGETED_RE = re.compile(r"(?:^|\s)(?:-k|--deselect)(?:[=\s]|$)|::|(?:^|\s)\S*\.py(?:::|\s|$)")
_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_FILE_WRITER_RE = re.compile(r"(?:^|[;&|])\s*(cat|tee)\b([^\n]*)")
_FILE_REDIRECT_RE = re.compile(r"(?<!<)>{1,2}\s*(?![(&])\S+")


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


def heavy_command_text(command: str) -> str:
    if "<<" not in command:
        return command_with_executable_separators(command)
    lines = command.splitlines(keepends=True)
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        result.append(line)
        opener = _HEREDOC_OPEN_RE.search(line)
        if opener is None:
            index += 1
            continue
        delimiter = opener.group(3)
        end = index + 1
        while end < len(lines):
            candidate = lines[end].rstrip("\r\n")
            if opener.group(1) == "-":
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                break
            end += 1
        if end == len(lines):
            result.extend(body.replace("`", "\n").replace("'", " ").replace('"', " ") for body in lines[index + 1 :])
            break
        segment_start = max(line.rfind(separator, 0, opener.start()) for separator in ";&|(){}`") + 1
        writer = _FILE_WRITER_RE.match(line[segment_start:])
        file_bound = False
        process_substitution = False
        if writer is not None:
            verb, args = writer.groups()
            file_bound = (
                bool(_FILE_REDIRECT_RE.search(args))
                if verb == "cat"
                else bool(re.search(r"(?:^|\s)(?:-a\s+)?[^\s<|;&-][^\s<|;&]*", args))
            )
            process_substitution = bool(re.search(r"[<>]\(", args))
        executable_body = not opener.group(2) and any("$(" in body or "`" in body for body in lines[index + 1 : end])
        if not file_bound or "|" in line[opener.end() :] or process_substitution or executable_body:
            result.extend(
                body.replace("`", "\n").replace("'", " ").replace('"', " ") for body in lines[index + 1 : end]
            )
            result.append(lines[end])
        else:
            result.append(lines[end])
        index = end + 1
    return command_with_executable_separators("".join(result))
