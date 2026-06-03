"""Forge-post body extraction for the AI-signature gate (#836 gate 15, #11).

The "No AI Signature on Posts Made on the User's Behalf" rule is enforced by
the ``handle_block_ai_signature`` PreToolUse gate in
``hooks/scripts/hook_router.py``. The matching (the position-anchored trailer
detection) lives in ``scripts/ai_signature_scan.py``; this module owns the
other half — deciding whether a Bash command is a forge-post surface and
pulling the body out of it.

It reuses the SAME canonical command parser (:mod:`teatree.hooks._command_parser`)
as the #1213 quote-scanner, #1415 banned-terms, and #1530 bare-reference gates,
rather than a second hand-rolled regex parser. The previous hand-rolled
extractor recognised only ``gh pr`` / ``glab mr`` and a QUOTED ``--body``, so an
AI-signature footer leaked on ``gh issue create/comment``, ``glab issue note``,
``glab mr note``, and the ``-b``/``-d``/heredoc body forms (#11 — the
souliane/skills#38 / #1840 / #1845 recurrence). Reusing the shared parser closes
the whole forge-post command class at once.

This module is the public seam the hook router (which lives outside the
``teatree`` package and so cannot import ``_command_parser`` directly) imports —
mirroring how ``banned_terms_scanner.extract_publish_payload`` wraps the same
parser for the banned-terms gate.
"""

from pathlib import Path

from teatree.hooks._command_parser import extract_bash_payload, is_fail_closed_sentinel, is_publish_command


def extract_forge_post_body(command: str, cwd: Path | None = None) -> str | None:
    """Return the scannable forge-post body of a Bash ``command``, or ``None``.

    ``None`` ⇒ ``command`` is not a forge-post / commit surface, or its body
    source is unresolvable (a missing / binary / unreadable ``-F`` file). The
    AI-signature gate's contract is fail-OPEN: an unresolvable body must never
    hard-block a forge post, so the parser's fail-closed sentinel is mapped back
    to ``None`` here (no scan, no block) instead of being scanned as text.

    :func:`is_publish_command` recognises the whole forge-post command class
    (``gh pr/issue create/edit/comment``, ``glab mr/issue create/update/note``,
    ``git commit``, and the ``gh``/``glab api`` WRITE paths). :func:`extract_bash_payload`
    pulls the body out of every flag form (``--body``/``--description``/
    ``--message``/``-b``/``-m``, ``--body-file``/``--file``/``-F``, ``-d``/
    ``--field`` JSON, heredocs). ``fail_closed_body_file=False`` keeps the
    fail-open posture on an unreadable ``gh``/``glab`` body file.
    """
    if not is_publish_command(command):
        return None
    payload = extract_bash_payload(command, fail_closed_body_file=False, cwd=cwd)
    if is_fail_closed_sentinel(payload):
        return None
    return payload
