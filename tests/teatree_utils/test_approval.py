"""Per-invocation interactive approval gate (#777).

The gate is the safety mechanism for destructive/expensive privileged
operations (fresh DEV dump fetch). It must be impossible for an
unattended agent to self-approve: approval requires reading an
interactive y/N confirmation from a TTY, and in a non-interactive /
no-TTY context the gate refuses with a clear "a human must run this"
message.

Unit-level is correct here: this is pure decision logic over one
unstoppable external (stdin/stdout TTY state), exactly the carve-out the
Test-Writing Doctrine reserves for unit tests. The integration coverage
(the flag actually threading through `db refresh`) lives in
test_django_db.py / the overlay suite.
"""

import io

import pytest

from teatree.utils.approval import ApprovalRefusedError, require_interactive_approval


class _FakeStream(io.StringIO):
    def __init__(self, *, tty: bool, content: str = "") -> None:
        super().__init__(content)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestRequireInteractiveApproval:
    def test_refuses_when_no_tty_agent_context(self) -> None:
        stdin = _FakeStream(tty=False, content="y\n")
        stdout = _FakeStream(tty=False)
        with pytest.raises(ApprovalRefusedError, match="human must"):
            require_interactive_approval("Pull fresh DEV dump?", stdin=stdin, stdout=stdout)

    def test_refuses_when_user_declines(self) -> None:
        stdin = _FakeStream(tty=True, content="n\n")
        stdout = _FakeStream(tty=True)
        with pytest.raises(ApprovalRefusedError, match="declined"):
            require_interactive_approval("Pull fresh DEV dump?", stdin=stdin, stdout=stdout)

    def test_refuses_on_empty_default_no(self) -> None:
        stdin = _FakeStream(tty=True, content="\n")
        stdout = _FakeStream(tty=True)
        with pytest.raises(ApprovalRefusedError):
            require_interactive_approval("Pull fresh DEV dump?", stdin=stdin, stdout=stdout)

    def test_grants_only_on_explicit_yes(self) -> None:
        stdin = _FakeStream(tty=True, content="yes\n")
        stdout = _FakeStream(tty=True)
        require_interactive_approval("Pull fresh DEV dump?", stdin=stdin, stdout=stdout)

    def test_prompt_is_shown_to_the_user(self) -> None:
        stdin = _FakeStream(tty=True, content="y\n")
        stdout = _FakeStream(tty=True)
        require_interactive_approval(
            "Pull fresh DEV dump for development-acme into ticket DB?",
            stdin=stdin,
            stdout=stdout,
        )
        assert "development-acme" in stdout.getvalue()

    def test_agent_cannot_self_approve_even_with_yes_in_buffer(self) -> None:
        # The decisive guard: a non-TTY stdin carrying "y" must NOT pass —
        # an unattended agent piping "y" is exactly what this blocks.
        stdin = _FakeStream(tty=False, content="y\ny\ny\n")
        stdout = _FakeStream(tty=False)
        with pytest.raises(ApprovalRefusedError, match="human must"):
            require_interactive_approval("Pull fresh DEV dump?", stdin=stdin, stdout=stdout)
