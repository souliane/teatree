"""Read-only "why did the leak gate SCAN this?" diagnostic for the deny path.

The banned-terms (#1415) and quote-scanner (#1213) gates enforce only on an
affirmatively-PUBLIC destination; everything else is meant to SKIP
(:func:`teatree.hooks.public_visibility.gate_skips_for_visibility`). Two shapes
defeat the skip for reasons that have nothing to do with the body, and neither
was ever NAMED in the refusal — so the agent read a leak block and had no way to
tell that the destination, not the text, was the problem:

**Chained publish.** The skip predicate walks EVERY top-level ``&&``/``;``/``|``
segment and refuses to skip when any of them carries an unrecognised leader, a
subshell, a command substitution or a transport construct. That is correct and
deliberate — an unrecognised segment can shell out to a second, unverifiable
publish — but it means a clean publish to a private repo is scanned whenever it
is chained with anything. The agent discovers this only by stalling.

**Unresolvable destination.** A flagless ``glab mr create`` / ``gh pr create``
takes its target from the git remote of the dir it runs in. When the hook's cwd
is not inside that repo, no destination resolves, the visibility is UNKNOWN, and
the gate fails closed (#3477) — so a post that could only ever land on a repo
``private_repos`` / ``internal_publish_namespaces`` already declares internal is
blocked as if it were public.

Both hints are pure TEXT appended to an already-decided refusal. Nothing here
resolves a verdict, widens a carve-out, or changes what any gate detects: the
same commands block, with a message that names the one-step fix. Every failure
inside is swallowed to ``""`` — a diagnostic must never be able to break the
gate it annotates.
"""

import shlex
from pathlib import Path

_ISSUE_ALONE = (
    "Re-issue the publish in its own Bash call, with no `&&`/`;`/pipe, no redirection, no subshell and no `$(…)`."
)

_CHAINED_HINT = (
    "\n\nWHY THIS SCANNED: the publish is CHAINED with other command segments, so the gate could not "
    "scope to it — an unrecognised chained segment could hide a second, unverifiable publish, so the "
    "whole command is scanned conservatively. Issued ALONE this publish would have been SKIPPED (its "
    f"destination is not public). {_ISSUE_ALONE}"
)

_WRAPPED_HINT = (
    "\n\nWHY THIS SCANNED: the publish is wrapped in a command substitution or subshell, so the gate "
    "cannot resolve which destination it targets and fails closed — the block is about the command's "
    f"SHAPE, not necessarily its body. {_ISSUE_ALONE}"
)

_UNRESOLVED_HINT = (
    "\n\nWHY THIS SCANNED: no publish destination could be resolved from this command — there is no "
    "`--repo`/`-R` flag, no forge URL, and the working directory the hook sees is not inside the "
    "target repo, so its git remote gave nothing. An unresolvable target is treated as PUBLIC and "
    "fails closed. If the target is private, name it explicitly and the gate will skip: add "
    "`--repo <owner/repo>` (or address the forge URL directly), and make sure the namespace is "
    "declared — `t3 <overlay> config_setting get private_repos` / `internal_publish_namespaces`."
)


def scan_scope_hint(command: str, cwd: Path | None) -> str:
    """Return a one-paragraph reason the leak gate scanned this command, or ``""``.

    ``""`` whenever the gate scanned for a reason this diagnostic cannot improve
    on — the destination really is public, the command is not a publish, or the
    scope is already unambiguous. Never raises.
    """
    if not command.strip():
        return ""
    try:
        return _hint(command, cwd)
    except Exception:  # noqa: BLE001 — a diagnostic must never break the gate it annotates
        return ""


def _hint(command: str, cwd: Path | None) -> str:
    from teatree.hooks import public_visibility, publish_destination  # noqa: PLC0415 — deferred: cold-hook import
    from teatree.hooks._command_parser import is_publish_command  # noqa: PLC0415 — deferred: cold-hook import
    from teatree.hooks._commit_repo_dir import segment_cwds  # noqa: PLC0415 — deferred: cold-hook import
    from teatree.hooks._gh_glab_hiding import command_segments  # noqa: PLC0415 — deferred: cold-hook import

    if public_visibility.gate_skips_for_visibility(command, cwd):
        return ""
    # Each publish is paired with the dir it ACTUALLY runs in — every `cd`/`pushd`
    # in the chain re-points it (mirrors `public_visibility.gate_skips_for_visibility`,
    # which already classifies each segment against its own effective cwd via
    # `segment_cwds`). Judging an isolated segment against the wrong dir (e.g. the
    # hook's ambient cwd) misjudges both whether it would skip alone AND which repo
    # a suggested retry would actually hit.
    publishes = [
        (shlex.join(words), segment_cwd)
        for words, segment_cwd in zip(command_segments(command), segment_cwds(command, cwd), strict=True)
        if words and is_publish_command(" ".join(words))
    ]
    if not publishes:
        # The segment splitter sees no publish yet the command IS one: the publish
        # sits inside a ``$(…)`` / subshell the splitter deliberately does not
        # descend into, so no destination can be scoped to it.
        return _WRAPPED_HINT if is_publish_command(command) else ""
    if len(publishes) == 1:
        isolated, segment_cwd = publishes[0]
        if public_visibility.gate_skips_for_visibility(isolated, segment_cwd):
            return _CHAINED_HINT + _isolated_suffix(command, isolated, segment_cwd)
    unresolved = (publish_destination.resolve_publish_destination(segment, cwd) is None for segment, cwd in publishes)
    if all(unresolved):
        return _UNRESOLVED_HINT
    return ""


def _isolated_suffix(command: str, isolated: str, segment_cwd: Path | None) -> str:
    """The isolated publish to re-issue, bound to the directory it must run FROM.

    ``isolated`` is rebuilt from the segment's decoded words, so it is a
    suggestion, not a quotation. A subshell's closing paren lands inside the last
    word and re-quoting it produces a command the agent must not paste. Requiring
    the rebuilt text to appear VERBATIM in the original is a cheap, sound proof
    that nothing was invented; when it does not, the generic instruction stands
    alone rather than offering a command that would not run.

    Bound to ``segment_cwd`` — the dir THIS segment actually runs in after every
    ``cd``/``pushd`` in the chain, never the hook's ambient cwd: a flagless
    ``gh``/``glab`` publish resolves its destination from the dir it runs in, so a
    retry issued from the wrong dir targets the wrong repo.
    """
    if isolated not in command:
        return ""
    if segment_cwd is None:
        return f"\n    {isolated}"
    return f"\n    (cd {segment_cwd} && {isolated})"
