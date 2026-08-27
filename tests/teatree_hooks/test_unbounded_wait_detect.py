"""Detecting an UNBOUNDED agent-authored wait loop (#3882).

An agent blocks on CI, a commit, or a background job by writing
``until <condition>; do sleep N; done``. The loop is a child of the session's
shell, so when the session ends the loop is reparented and keeps polling — and
the condition it waits on may never become true, because whatever it waited for
was finished by someone else or abandoned. With no deadline that is forever.

The detector is pure command analysis, so the gate that drives it neither reads
process state nor decides anything is dead: it refuses to CREATE an unbounded
wait rather than hunting for one afterwards.
"""

import pytest

from teatree.hooks.unbounded_wait_detect import detect_unbounded_wait


class TestUnboundedWaitsAreDetected:
    @pytest.mark.parametrize(
        "command",
        [
            # The exact shapes agents write to block on external work.
            "until gh pr checks 3882 | grep -q pass; do sleep 180; done",
            "until [ \"$(pgrep -fc 'uv run pytest')\" -le 1 ]; do sleep 15; done",
            "until ! pgrep -f 'dev/push-gate.sh' > /dev/null; do sleep 20; done; echo DONE",
            "until git log --oneline | grep -q 3528; do sleep 5; done",
            "until grep -q EXIT= /tmp/suite.log; do sleep 45; done",
            "while ! test -f /tmp/done; do sleep 10; done",
            "while true; do gh pr view 1 --json state; sleep 60; done",
            # Multi-line form.
            "until curl -sf localhost:8000\ndo\n  sleep 30\ndone",
            # Wrapped in a nested shell with no bound anywhere.
            "bash -c 'until gh pr checks 1 | grep -q pass; do sleep 60; done'",
        ],
    )
    def test_a_wait_with_no_deadline_is_flagged(self, command: str) -> None:
        detection = detect_unbounded_wait(command)
        assert detection.is_unbounded_wait is True
        assert detection.message

    def test_the_message_names_a_runnable_bounded_rewrite(self) -> None:
        detection = detect_unbounded_wait("until gh pr checks 1; do sleep 60; done")
        assert "timeout" in detection.message
        assert "SECONDS" in detection.message
        assert "PPID" in detection.message

    def test_waiting_on_another_process_is_not_a_bound(self) -> None:
        # `while kill -0 "$job"` waits exactly as long as that job hangs, which is
        # the runaway itself. Only the loop's OWN parent ($PPID) bounds its lifetime.
        assert detect_unbounded_wait('while kill -0 "$job" 2>/dev/null; do sleep 10; done').is_unbounded_wait


class TestBoundedWaitsPassThrough:
    @pytest.mark.parametrize(
        "command",
        [
            # A hard deadline: the wait exits on its own and says why.
            "timeout 1800 bash -c 'until gh pr checks 1 | grep -q pass; do sleep 60; done'",
            "timeout 30m bash -c 'until test -f /tmp/done; do sleep 20; done' || echo TIMED OUT",
            # An in-shell deadline off the bash elapsed-seconds builtin.
            'SECONDS=0; until test -f /tmp/done || [ "$SECONDS" -ge 900 ]; do sleep 20; done',
            # An epoch deadline computed with date.
            'end=$(( $(date +%s) + 600 )); while [ "$(date +%s)" -lt "$end" ]; do sleep 20; done',
            # Lifetime tied to the spawning session: the wait ends when nobody is
            # left to read the answer. The loop exits ITSELF; nothing is signalled.
            'until test -f /tmp/done || ! kill -0 "$PPID" 2>/dev/null; do sleep 20; done',
        ],
    )
    def test_a_bounded_wait_is_not_flagged(self, command: str) -> None:
        assert detect_unbounded_wait(command).is_unbounded_wait is False


class TestNonWaitsPassThrough:
    @pytest.mark.parametrize(
        "command",
        [
            "",
            "sleep 300",  # bounded by construction — one nap, not a loop
            "gh pr checks 3882",
            "for i in $(seq 1 20); do gh pr checks 1; sleep 10; done",  # finite iteration list
            'while read -r line; do echo "$line"; done < /tmp/list',  # bounded by its input
            'echo "to wait: until gh pr checks 1; do sleep 60; done"',  # quoted prose, not a loop
            "grep -rn 'until .*sleep' src/",
            "uv run pytest tests/ -q",
        ],
    )
    def test_an_ordinary_command_is_not_flagged(self, command: str) -> None:
        assert detect_unbounded_wait(command).is_unbounded_wait is False


class TestABoundBelongsToTheWaitItWraps:
    """A ``timeout`` on one command does not vouch for a loop in another.

    The shape was collapsed into one whole-command verdict, so any ``timeout``
    anywhere in the shell string marked every later loop bounded — and the
    natural way to write a runaway is to bound the setup step and not the wait.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 5 gh pr checks 1; until gh pr checks 1 | grep -q pass; do sleep 60; done",
            "timeout 30 bash -c 'gh pr view 1' && while ! test -f /tmp/done; do sleep 10; done",
        ],
    )
    def test_an_unrelated_timeout_does_not_bound_a_later_loop(self, command: str) -> None:
        assert detect_unbounded_wait(command).is_unbounded_wait is True

    def test_the_wrapped_loop_stays_bounded_alongside_an_unbounded_sibling(self) -> None:
        # The FIRST wait is genuinely bounded; the SECOND is not, and that is
        # what the command must be judged on.
        command = "timeout 60 bash -c 'until test -f /tmp/a; do sleep 5; done'; until test -f /tmp/b; do sleep 5; done"
        assert detect_unbounded_wait(command).is_unbounded_wait is True

    def test_a_timeout_wrapped_loop_alone_is_still_allowed(self) -> None:
        command = "timeout 60 bash -c 'until test -f /tmp/a; do sleep 5; done'; echo done"
        assert detect_unbounded_wait(command).is_unbounded_wait is False
