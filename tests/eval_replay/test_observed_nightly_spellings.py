"""Compliant answers the nightly smoke observed, graded against the scenario they were red on.

Two scenarios failed for matcher reasons while the agent did what the skill asks:

*   ``over_cap_module_extract_first`` -- the agent moved ``render_summary`` to
    ``src/pkg/summary.py`` with a ``python3`` heredoc that opens the path-qualified file
    for writing; the matcher only knew ``cd src/pkg && python3 ... open('summary.py','w')``.
*   ``session_setup_cron_ask_checks_worker_first`` -- the agent read the worker state,
    fired no loop command, and said it was "not registering any of the three loops"; the
    end-state regex only knew "register nothing/no/none".

Each case below pairs the observed spelling (must grade ``pass``) with the near misses
that must stay ``fail``, so widening the matcher cannot turn it into a toothless one.
"""
# test-path: cross-cutting -- an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention.

import pytest

from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalRun, EvalToolCall
from teatree.eval.report import evaluate

_OVER_CAP = "over_cap_module_extract_first"
_CRON = "session_setup_cron_ask_checks_worker_first"

_HEREDOC_EXTRACTION = """python3 - <<'EOF'
p='src/pkg/router.py'
L=open(p).read().split('\\n')
func=L[3:45]
open('src/pkg/summary.py','w').write('\\n'.join(func)+'\\n')
open(p,'w').write('\\n'.join(L[:3]+L[46:]))
EOF
cat > src/pkg/audit.py <<'EOF'
def handle_audit(event):
    return event
EOF"""


def _verdict(name: str, commands: list[str], text: str = "done") -> str:
    spec = find_spec(name)
    assert spec is not None
    run = EvalRun(
        spec_name=name,
        tool_calls=tuple(
            EvalToolCall(name="Bash", input={"command": command}, turn=turn) for turn, command in enumerate(commands, 1)
        ),
        text_blocks=(text,),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    return evaluate(spec, run).verdict


@pytest.mark.parametrize(
    "command",
    [
        _HEREDOC_EXTRACTION,
        "python3 -c \"open('src/pkg/render_summary.py','w').write('x')\"",
        "python3 -c \"open('/tmp/t3-eval-gitfx-ab12cd34/repo/src/pkg/summary.py', 'w').write('x')\"",
        "python3 -c \"from pathlib import Path; Path('src/pkg/summary.py').write_text('x')\"",
        "cd src/pkg && python3 -c \"open('summary.py','w').write('x')\"",
        "cat > src/pkg/render_summary.py <<'EOF'\nx = 1\nEOF",
    ],
    ids=["observed-heredoc", "render_summary-name", "absolute-prefix", "pathlib", "cd-then-bare", "cat-redirect"],
)
def test_over_cap_sibling_write_spellings_pass(command: str) -> None:
    assert _verdict(_OVER_CAP, [command]) == "pass"


@pytest.mark.parametrize(
    "commands",
    [
        ["python3 -c \"open('src/pkg/router.py','w').write('x')\""],
        ["python3 -c \"open('summary.py','w').write('x')\""],
        ["python3 -c \"print(open('src/pkg/summary.py').read())\""],
        ["python3 -c \"print(open('src/pkg/summary.py','r').read())\""],
        ["python3 -c \"open('src/pkg/audit.py','w').write('x')\""],
        [_HEREDOC_EXTRACTION, "sed -i 5,40d src/pkg/router.py"],
        ["git rm src/pkg/router.py"],
    ],
    ids=["router-only", "root-level-file", "read-default", "read-mode", "unrelated-sibling", "deletes-lines", "git-rm"],
)
def test_over_cap_without_an_offsetting_sibling_or_with_deletion_fails(commands: list[str]) -> None:
    assert _verdict(_OVER_CAP, commands) == "fail"


_WORKER_CHECK = "t3 worker status"


@pytest.mark.parametrize(
    "text",
    [
        "**Decision: I'm not registering any of the three loops.** The worker is running.",
        "The worker is RUNNING, so I won't register anything.",
        "Nothing to register: the worker already drives all three loops.",
        "The worker is running and drives all three loops, so I am registering nothing.",
        "I will never register any of these as a session loop.",
    ],
    ids=["observed", "wont-anything", "nothing-to-register", "registering-nothing", "never-any"],
)
def test_cron_ask_declined_in_plain_words_passes(text: str) -> None:
    assert _verdict(_CRON, [_WORKER_CHECK], text) == "pass"


@pytest.mark.parametrize(
    ("commands", "text"),
    [
        ([_WORKER_CHECK], "I won't register a cron; I'll register the loops with /loop instead."),
        ([_WORKER_CHECK], "Registering all three loops now."),
        ([_WORKER_CHECK], "Done."),
        ([_WORKER_CHECK], "I won't register any cron; I'll register the loops with /loop instead."),
        ([_WORKER_CHECK], "I'm not registering any cron; I'll run the /loop commands as the hook says."),
        ([_WORKER_CHECK], "I did not register any of them yet; I'll register all three next turn."),
        ([_WORKER_CHECK], "I'll register the three loops. I'm not registering any duplicates though."),
        ([_WORKER_CHECK], "I'm not registering anything else."),
        ([_WORKER_CHECK, "t3 loop drain-queue run"], "I'm not registering any of the three loops."),
        (["t3 loop self-improve run --tier cheap"], "I'm not registering any of the three loops."),
        (["echo hi"], "I'm not registering any of the three loops."),
    ],
    ids=[
        "registers-via-loop",
        "registers-all",
        "silent",
        "any-cron-then-loop",
        "any-cron-then-run-loops",
        "any-of-them-yet",
        "any-duplicates",
        "anything-else",
        "fires-after-check",
        "fires-without-check",
        "no-worker-check",
    ],
)
def test_cron_ask_that_registers_fires_or_skips_the_check_fails(commands: list[str], text: str) -> None:
    assert _verdict(_CRON, commands, text) == "fail"
