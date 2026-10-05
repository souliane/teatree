"""Off-the-foreground scenario builder kept separate from the main catalog."""

import dataclasses

from scripts.eval.corpus_gen.model import Call, Expect, Scenario, any_of, match, negative


def _bash(command: str) -> Call:
    return Call(tool="Bash", args={"command": command, "description": "step"})


def _bg_bash(command: str) -> Call:
    return Call(tool="Bash", args={"command": command, "description": "bg", "run_in_background": True})


@dataclasses.dataclass(frozen=True)
class BgSpec:
    """Declarative shape of a 'do the long op off the foreground' scenario.

    Passes on a ``Task`` (prompt matches ``keyword``), a backgrounded ``Bash`` (``bg_cmd``), or a
    ``Monitor`` on a real watch command, persistent or not. A commandless ``Monitor`` whose
    description names the operation must be persistent; an unrelated ``Monitor`` always fails.
    When ``fg_cmd`` is given, a negative matcher forbids a foreground sleep-poll.

    ``keyword`` is a CONTENT keyword (matched against a ``Task`` prompt, which
    legitimately describes the job in prose). ``monitor_watch`` is a COMMAND-SHAPE
    regex (matched against a ``Monitor`` command, which is an actual shell-style
    command) — tight watch semantics so ``echo pipeline`` does NOT pass and a bare
    ``ci`` inside a word like ``decision`` does NOT match.
    """

    name: str
    desc: str
    agent: str
    prompt: str
    keyword: str
    bg_cmd: str
    yaml_file: str
    fg_cmd: str | None = None
    monitor_watch: str = ""


#: A FOREGROUND sleep-poll. ``until`` sits beside ``while`` because the two spell the
#: same waiter and a corpus that named only one let a real foreground poll pass. The
#: exemption below is what keeps this from contradicting the ``any_of`` above it: a
#: backgrounded waiter is the sanctioned shape, so the negative must not kill the very
#: call the positive blesses.
_SLEEP_POLL = r"(?i)((while|until) .*sleep|watch -n|for i in.*sleep|sleep \d+; *(gh|glab))"
_BACKGROUNDED = match("Bash", "run_in_background", "(?i)true")

#: A CI / job WATCH command shape for the ``Monitor`` branch. Requires a real
#: watch verb (``gh run watch|view|list``, ``gh pr checks``, ``glab ci|pipeline``,
#: ``gh workflow``) or a ``until|while`` loop driving one — matching the real
#: metered transcripts (``until gh run list … grep completed``, ``gh run watch``)
#: while rejecting ``echo pipeline`` and ``ci`` inside an unrelated word. The
#: default ``BgSpec.monitor_watch`` when a scenario declares none.
_MONITOR_CI_WATCH = (
    r"(?i)(gh run (watch|view|list)|gh pr checks|glab (ci|pipeline)|gh workflow|"
    r"(until|while)\b.*(gh run|glab|gh pr checks))"
)


def background_scenario(spec: BgSpec) -> Scenario:
    monitor_watch = spec.monitor_watch or _MONITOR_CI_WATCH
    persistent = match("Monitor", "persistent", "(?i)true")
    expects: list[Expect] = [
        any_of(
            (
                match("Monitor", "command", monitor_watch),
                match("Monitor", "description", spec.keyword),
                match("Task", "prompt", spec.keyword),
                _BACKGROUNDED,
            ),
            pass_call=_bg_bash(spec.bg_cmd),
        ),
        negative(
            match("Monitor", "description", spec.keyword),
            fail_call=Call(tool="Monitor", args={"description": spec.desc, "persistent": False}),
            unless=persistent,
        ),
    ]
    if spec.fg_cmd is not None:
        expects.append(
            negative(match("Bash", "command", _SLEEP_POLL), fail_call=_bash(spec.fg_cmd), unless=_BACKGROUNDED)
        )
    return Scenario(
        name=spec.name,
        scenario=spec.desc,
        agent_path=spec.agent,
        prompt=spec.prompt,
        expects=tuple(expects),
        tools=("Bash", "Task", "Monitor"),
        yaml_file=spec.yaml_file,
    )
