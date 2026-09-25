"""The declared sandbox, on the lanes that drive a model without the Claude CLI.

A scenario's ``fixture`` / ``cli_stubs`` declarations describe the world its prompt
presupposes — a repo whose changes are staged, a wired ``t3`` on ``PATH``. The
CLI-backed ``api`` lane provisions both and lets its CLI child run the agent's
commands for real. The two non-CLI lanes registered EVERY tool as an inert stub
returning ``""``, so on those lanes the declarations reached nothing: 44 of the 266
shipped scenarios ran against an agent whose first probe told it the shell answered
nothing at all, and it spent its turns establishing that rather than doing the graded
thing. Three of them pass on ``--backend api`` and failed 0/3 here.

``Bash`` executes for a declared sandbox. ``Edit`` executes only for a declared
fixture, so a model can verify its change in the same throwaway tree. Other tools
stay inert because the eval grades the CALL. The shell gets no model credential.
"""

import dataclasses
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from teatree.eval.api_runner import resolve_agent_path
from teatree.eval.cli_stub_fixture import prepend_to_path, provision_cli_stubs
from teatree.eval.git_fixture import provision_fixture
from teatree.eval.isolation import isolated_claude_env
from teatree.eval.models import EvalSpec
from teatree.llm.credentials import AnthropicApiKeyCredential
from teatree.utils.run import SUBPROCESS_UNREACHABLE, CompletedProcess, TimeoutExpired, run_allowed_to_fail

#: Wall clock for ONE sandboxed command. The scenario's own watchdog bounds the whole
#: drive; this stops a single hung command from consuming all of it.
COMMAND_TIMEOUT_SECONDS = 60

#: Output cap per command. A stub prints one line and a fixture's ``git log`` a few, so
#: anything far larger is a runaway whose tail the model does not need to proceed.
MAX_OUTPUT_CHARS = 4000

#: Both model credentials, derived from the credential that names them rather than
#: spelled out, so a renamed variable travels here.
_STRIPPED_VARS = (
    AnthropicApiKeyCredential().spec.env_var,
    *AnthropicApiKeyCredential().spec.conflicting_vars,
)

#: Canonical names of tools that can act on a declared sandbox.
BASH_TOOL = "Bash"
EDIT_TOOL = "Edit"


@dataclasses.dataclass(frozen=True, slots=True)
class EvalSandbox:
    """The world a scenario declared, made real: a cwd to act in and a ``PATH`` to act with."""

    cwd: Path
    env: dict[str, str]
    can_edit: bool = False

    def run_bash(self, **kwargs: object) -> str:
        """Run the model's command in the sandbox and answer with what a shell would show."""
        command = kwargs.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Bash: no command given"
        try:
            result = run_allowed_to_fail(
                ["/bin/sh", "-c", command],
                expected_codes=None,
                env=self.env,
                cwd=self.cwd,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            return f"Bash: killed after {COMMAND_TIMEOUT_SECONDS}s — the command was still running"
        except SUBPROCESS_UNREACHABLE as exc:
            return f"Bash: could not run the command: {exc}"
        return _render(result)

    def run_edit(self, **kwargs: object) -> str:
        """Apply a Claude-style exact replacement inside the declared fixture only."""
        if not self.can_edit:
            return "Edit: no editable fixture declared"
        file_path = kwargs.get("file_path")
        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        replace_all = kwargs.get("replace_all", False)
        if not isinstance(file_path, str) or not file_path:
            return "Edit: no file_path given"
        if not isinstance(old_string, str) or not isinstance(new_string, str) or not isinstance(replace_all, bool):
            return "Edit: invalid replacement arguments"
        root = self.cwd.resolve()
        requested = Path(file_path)
        try:
            target = (requested if requested.is_absolute() else root / requested).resolve()
        except (OSError, RuntimeError) as exc:
            return f"Edit: could not resolve path: {exc}"
        if not target.is_relative_to(root):
            return "Edit: path is outside the declared fixture"
        return (
            _create_file(target, new_string)
            if not old_string
            else _replace_file(target, old_string, new_string, replace_all=replace_all)
        )


def _create_file(target: Path, content: str) -> str:
    if target.exists():
        return "Edit: file already exists"
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Edit: could not create file: {exc}"
    return f"Created {target}"


def _replace_file(target: Path, old: str, new: str, *, replace_all: bool) -> str:
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return f"Edit: could not read file: {exc}"
    matches = content.count(old)
    if matches == 0:
        return "Edit: old_string not found"
    if matches > 1 and not replace_all:
        return f"Edit: old_string has {matches} matches; pass replace_all=true"
    try:
        target.write_text(content.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
    except OSError as exc:
        return f"Edit: could not write file: {exc}"
    return f"Edited {target}"


def sandbox_bash_tool(sandbox: EvalSandbox) -> Callable[..., str]:
    """The toolset's ``Bash`` body for *sandbox* — a plain function, which a bound method is not."""

    def bash(**kwargs: object) -> str:
        return sandbox.run_bash(**kwargs)

    return bash


def sandbox_edit_tool(sandbox: EvalSandbox) -> Callable[..., str]:
    """The toolset's ``Edit`` body for a declared fixture."""

    def edit(**kwargs: object) -> str:
        return sandbox.run_edit(**kwargs)

    return edit


def _render(result: CompletedProcess[str]) -> str:
    """Stdout and stderr as one body, capped, with the exit status named."""
    body = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if len(body) > MAX_OUTPUT_CHARS:
        body = f"{body[:MAX_OUTPUT_CHARS]}\n… truncated at {MAX_OUTPUT_CHARS} characters"
    return f"exit={result.returncode}\n{body or '(no output)'}"


@contextmanager
def provision_eval_sandbox(spec: EvalSpec) -> Iterator[EvalSandbox | None]:
    """Yield the sandbox *spec* declared, or ``None`` when it declared none.

    ``None`` is the untouched shape: a scenario declaring neither field keeps the fully
    inert toolset it had before this module existed.
    """
    if not (spec.fixture or spec.cli_stubs):
        yield None
        return
    with ExitStack() as stack:
        env, home = stack.enter_context(isolated_claude_env(_STRIPPED_VARS))
        if spec.cli_stubs:
            env = prepend_to_path(
                env,
                stack.enter_context(provision_cli_stubs(spec.cli_stubs, source_path=spec.source_path)),
            )
        yield EvalSandbox(cwd=_sandbox_cwd(stack, spec, home), env=env, can_edit=bool(spec.fixture))


def _sandbox_cwd(stack: ExitStack, spec: EvalSpec, home: str) -> Path:
    """The declared fixture's directory, else the neutral isolated home."""
    if not spec.fixture:
        return Path(home)
    skill_path = resolve_agent_path(spec.agent_path, spec.source_path.parent)
    return stack.enter_context(provision_fixture(spec.fixture, skill_path=skill_path))


__all__ = [
    "BASH_TOOL",
    "COMMAND_TIMEOUT_SECONDS",
    "EDIT_TOOL",
    "MAX_OUTPUT_CHARS",
    "EvalSandbox",
    "provision_eval_sandbox",
    "sandbox_bash_tool",
    "sandbox_edit_tool",
]
