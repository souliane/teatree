"""Inert CLI stubs for clean-room scenarios whose correct command needs a wired binary.

A single-action probe whose CORRECT command is ``t3 <overlay> notify send …``,
``t3 <overlay> lifecycle record-e2e-run … --posted-url …``, or a forge
``gh pr diff`` runs in a sandbox with no wired overlay CLI and no network, so that
command ERRORS. The agent then investigates the failure across many turns instead
of stopping — and the clean-room :data:`~teatree.eval.models.CLEAN_ROOM_MIN_TURNS`
floor plus the #2192 cap-taint turn that wander into a ``max_turns`` red even
though the matcher already matched the correct call. The command, not the model,
manufactured the red.

Declaring ``cli_stubs: [t3, gh, glab]`` prepends a throwaway ``bin/`` of inert
``sh`` stubs to the scenario's ``PATH``. Each stub accepts any sanctioned argv,
prints one plausible success line per verb family, and exits ``0`` — no state, no
network. The correct command now succeeds, so the agent stops.

The stubs are **inert**: the matchers grade the CALL the agent made, never the
stub's output, so a forbidden invocation (a merge, an e2e bypass, a review-request
post) is still captured and still reds — the negatives keep full teeth. This
mirrors ``fixture: git_repo`` (:mod:`teatree.eval.git_fixture`): the skill is
correct, the sandbox just lacked the wiring the prompt presupposes. The field is a
SEPARATE lever from ``fixture:`` so it composes with ``fixture: git_repo`` (a
scenario can declare both); an empty/absent ``cli_stubs`` leaves ``PATH`` untouched,
so every existing scenario is byte-identical to before this field existed.
"""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

#: ``t3`` stub — one success line per sanctioned verb family, exit 0. The verb
#: families are the ones the opted-in scenarios (and the canary) actually issue:
#: the self-DM notify, the on-behalf post-receipt notify, the directive capture,
#: the e2e attestation and test-plan post, the on-behalf review post-comment, and
#: the review/reaction verbs the (currently green) review probes would use. An
#: unrecognised verb still exits 0 with a neutral line so a stray discovery call
#: (``t3 --help``) never errors the agent back into a wander.
_T3_STUB = """\
#!/bin/sh
# Inert teatree CLI stub for clean-room evals — prints a plausible success line
# per verb family and exits 0. No state, no network. See cli_stub_fixture.py.
args=" $* "
case "$args" in
    *" notify send "*|*" notify dm "*) echo "DM queued (idempotency key accepted)" ;;
    *" notify post "*) echo "posted to channel" ;;
    *" directive capture "*) echo "captured directive #1 (state=captured)." ;;
    *" lifecycle record-e2e-run "*) echo "recorded e2e run (attestation stored)" ;;
    *" e2e post-test-plan "*) echo "test plan posted to the ticket" ;;
    *" review record "*) echo "recorded verdict (bound to the reviewed head sha)" ;;
    *" review post-comment "*) echo "posted review comment (as the user)" ;;
    *" review-request check "*) echo "MR is review-requestable" ;;
    *" slack react "*) echo "reaction added" ;;
    *) echo "ok" ;;
esac
exit 0
"""

#: ``gh`` stub — the pr ``diff``/``view`` verbs print a small static unified diff /
#: PR summary; ``pr create`` prints a plausible URL. Everything exits 0.
_GH_STUB = """\
#!/bin/sh
# Inert GitHub CLI stub for clean-room evals. See cli_stub_fixture.py.
args=" $* "
case "$args" in
    *" pr diff "*) printf 'diff --git a/app.py b/app.py\\n@@ -1 +1 @@\\n-old = 1\\n+new = 2\\n' ;;
    *" pr view "*) echo "PR #1 — Example change (open, mergeable, CI green)" ;;
    *" pr create "*) echo "https://github.com/example/repo/pull/1" ;;
    *) echo "ok" ;;
esac
exit 0
"""

#: ``glab`` stub — the mr ``diff``/``view`` verbs print a small static unified diff /
#: MR summary; ``mr create`` prints a plausible URL. Everything exits 0.
_GLAB_STUB = """\
#!/bin/sh
# Inert GitLab CLI stub for clean-room evals. See cli_stub_fixture.py.
args=" $* "
case "$args" in
    *" mr diff "*) printf 'diff --git a/app.py b/app.py\\n@@ -1 +1 @@\\n-old = 1\\n+new = 2\\n' ;;
    *" mr view "*) echo "MR !1 — Example change (open, mergeable, CI green)" ;;
    *" mr create "*) echo "https://gitlab.example.com/example/repo/-/merge_requests/1" ;;
    *) echo "ok" ;;
esac
exit 0
"""

#: The stub bodies keyed by the name the scenario declares in ``cli_stubs:``. A
#: name outside this set is a spec error (fails loud at parse time in the loader),
#: never a silently-missing stub.
KNOWN_CLI_STUBS: dict[str, str] = {"t3": _T3_STUB, "gh": _GH_STUB, "glab": _GLAB_STUB}


@contextmanager
def provision_cli_stubs(names: Sequence[str]) -> Iterator[Path]:
    """Yield a throwaway ``bin/`` dir holding an executable stub for each name.

    The directory (and every stub in it) is removed when the context exits. Prepend
    the yielded dir to the child's ``PATH`` (see :func:`prepend_to_path`) so the
    agent's ``t3``/``gh``/``glab`` invocations resolve to the inert stub instead of
    erroring on a missing binary.
    """
    unknown = [n for n in names if n not in KNOWN_CLI_STUBS]
    if unknown:
        msg = f"unknown cli_stubs: {unknown} (known: {sorted(KNOWN_CLI_STUBS)})"
        raise ValueError(msg)
    with TemporaryDirectory(prefix="t3-eval-clistub-") as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        for name in names:
            stub = bindir / name
            stub.write_text(KNOWN_CLI_STUBS[name], encoding="utf-8")
            stub.chmod(0o755)
        yield bindir


def prepend_to_path(env: dict[str, str], bindir: Path) -> dict[str, str]:
    """Return a copy of *env* with *bindir* prepended to ``PATH`` (highest priority)."""
    new = dict(env)
    existing = new.get("PATH", "")
    new["PATH"] = f"{bindir}{os.pathsep}{existing}" if existing else str(bindir)
    return new


__all__ = ["KNOWN_CLI_STUBS", "prepend_to_path", "provision_cli_stubs"]
