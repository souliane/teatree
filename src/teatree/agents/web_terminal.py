"""Build the ``claude`` argv the ttyd web terminal wraps (#3162 — resurrected).

A fresh ``claude`` session, or ``claude --resume <session-id>`` when the debug
button is opened from a card/task carrying a Claude session UUID. The pre-#541
version pre-loaded a full interactive system-context via the skill-bundle/prompt
machinery that #541 removed; the resurrected debug button starts a plain session
in the teatree checkout instead, which is all the "poke a stuck ticket" use case
needs and couples the dashboard to none of that removed machinery.
"""

import re
import shutil

from teatree.agents.terminal_launcher import LaunchResult, launch_ttyd

# A Claude session id is a UUID; anything else is rejected so a card's free-form
# label can never be shell-injected into the resume argv.
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def build_claude_command(resume_session_id: str = "") -> list[str]:
    """The ``claude`` argv: a fresh session, or ``--resume <uuid>`` when one is given.

    Raises ``FileNotFoundError`` when the ``claude`` CLI is absent and ``ValueError``
    when a non-UUID resume id is passed (guards the ttyd argv against injection).
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)
    if resume_session_id:
        if not _SESSION_ID_RE.match(resume_session_id):
            msg = f"not a valid claude session id: {resume_session_id!r}"
            raise ValueError(msg)
        return [claude_bin, "--resume", resume_session_id]
    return [claude_bin]


def launch_web_session(resume_session_id: str = "") -> LaunchResult:
    """Spawn a loopback ttyd terminal wrapping a fresh or resumed ``claude`` session."""
    return launch_ttyd(build_claude_command(resume_session_id))
