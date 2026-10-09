"""Fold preservation — a ticket's substance outlives its row (#4344).

The backlog sweep's default posture is to group aggressively and close nothing for
real: a member ticket's row is retired only after its body has moved into an existing
host ticket. "For real" is the load-bearing word — a close that discarded the member's
content is a lost idea, and by the time anyone notices it is indistinguishable from a
legitimate fold.

:func:`fold_body` is the constructive half (produce a host body carrying the member
verbatim); :func:`check_fold_preserved` is the gate half (prove a host body — a
hand-edited one, or one re-read back off the forge — still carries it). Both are pure
over their inputs, the same shape as :mod:`teatree.core.gates.bulk_close_gate`, so the
residual is the CLI boundary (``t3 <overlay> ticket fold`` / ``fold-check``) rather
than anything this module can close on its own.
"""

_PREVIEW_CAP = 5


def _substantive_lines(body: str) -> list[str]:
    """Non-blank lines with internal whitespace collapsed — what a fold must carry over."""
    return [" ".join(line.split()) for line in body.splitlines() if line.strip()]


def fold_marker(member_ref: str) -> str:
    """The heading line recording *member_ref*'s substance inside a host body."""
    return f"## Folded in: {member_ref}"


def fold_body(*, host_body: str, member_ref: str, member_title: str, member_body: str) -> str:
    """*host_body* with the member's substance appended verbatim under its own heading.

    Idempotent by marker: a host already carrying :func:`fold_marker` for this ref is
    returned unchanged, so a retried sweep stacks no duplicate copies of the member.
    """
    marker = fold_marker(member_ref)
    if marker in host_body:
        return host_body
    heading = f"{marker} — {member_title.strip()}" if member_title.strip() else marker
    parts = [part for part in (host_body.rstrip(), heading, member_body.strip()) if part]
    return "\n".join(("\n\n".join(parts), ""))


def check_fold_preserved(*, member_body: str, host_body: str) -> str:
    """Return a non-empty refusal when *host_body* has lost part of *member_body*.

    Comparison is per non-blank line with internal whitespace collapsed, so a
    re-indented fold still passes while a summarised one — the close that quietly
    discards a row — is refused. Returns ``""`` (proceed) when every line survived.
    """
    surviving = set(_substantive_lines(host_body))
    missing = [line for line in _substantive_lines(member_body) if line not in surviving]
    if not missing:
        return ""
    preview = "; ".join(missing[:_PREVIEW_CAP]) + (" ..." if len(missing) > _PREVIEW_CAP else "")
    return (
        f"Refusing the fold: {len(missing)} line(s) of the member's body are absent from the host "
        f"body, so this fold discarded content instead of moving it — retiring the row now would "
        f"lose the idea. Missing: {preview}. Re-fold with `t3 <overlay> ticket fold` (it copies the "
        f"body verbatim) and re-check before retiring anything."
    )
