r"""Destination-KIND classification for the bare-reference link gate (#1530).

The bare-reference gate forces every reference into a clickable markdown
link ``[#N](url)``. That is the right rule for a USER-FACING surface --
a Slack DM to the operator, a ``t3 notify send``, the assistant's own
chat -- where a bare ``#1764`` is unclickable noise and even a bare URL
should be left clickable rather than rewritten.

It is the WRONG rule for an EXTERNAL FORGE surface. GitHub/GitLab render
a bare ``#1764`` / ``!7546`` as a live cross-reference automatically, so
the bare id is not only fine, it is PREFERRED there -- a ``gh pr create``
that the gate forced into markdown is exactly the over-block this module
fixes.

This module classifies the publish DESTINATION KIND of a command and lets
the gate relax for external-forge posts while still enforcing on
user-facing ones.

- :data:`DestinationKind.EXTERNAL_FORGE` -- a structured ``gh``/``glab``
    issue/PR/MR create/comment/edit/note/update, a forge ``api`` WRITE
    (POST/PATCH/PUT/...), or a t3 forge wrapper
    (``review post-comment``, ``review post-draft-note``,
    ``ticket create-issue``). The forge auto-links refs; the gate does
    NOT enforce -- both bare ids and bare URLs are allowed.
- :data:`DestinationKind.USER_FACING` -- everything else a publish can
    target that a human reads: a Slack send, ``t3 notify send``,
    ``t3 slack react``, a ``chat.postMessage`` curl, a ``git commit``
    message, or an UNCLASSIFIABLE command. The gate enforces (bare id ->
    block, bare URL -> allow, markdown -> allow).

The classification is FAIL-SAFE toward the user-facing direction: a
command is EXTERNAL_FORGE only when at least one publish segment is
provably an external-forge post and NO segment publishes to anything
else. A single user-facing or unclassifiable publish segment (or no
publish segment at all) makes the whole command USER_FACING, so the
strict gate keeps firing unless the destination is CLEARLY an external
forge -- mirroring the prove-or-fail-safe posture of the sibling
private-repo destination skip (:mod:`teatree.hooks.publish_destination`).

This is a SEPARATE axis from the public/private destination skip in
:mod:`teatree.hooks.publish_destination`: that one decides whether to
scan a PUBLIC vs PROVABLY-INTERNAL target at all; this one decides, for a
target that IS scanned, whether the FORGE will render the ref (relax) or
a HUMAN will read it raw (enforce).
"""

from enum import StrEnum
from typing import Final

from teatree.hooks._publish_detection import segment_is_api_write, segment_word_lists
from teatree.hooks.publish_surface import _GH_ELIGIBLE_VERBS, _GLAB_ELIGIBLE_VERBS, _strip_cd_prefix

# A posting segment is ``<tool> <sub> <verb>`` at minimum (``gh pr create``).
_FORGE_POSTING_WORD_COUNT: Final[int] = 3

# t3 verb-segment substrings whose destination is an EXTERNAL FORGE (a
# filed issue, a posted PR/MR comment, a draft review note). Mirrors the
# forge-bound entries of ``_command_parser._T3_PUBLISH_SUBSTRINGS``;
# ``notify send`` and ``slack react`` are deliberately EXCLUDED -- they
# target the user's Slack, a USER-FACING surface.
_T3_FORGE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "review post-comment",
    "review post-draft-note",
    "ticket create-issue",
)

# Markers whose presence in a non-forge publish segment makes it a
# USER-FACING post: a ``chat.postMessage`` curl, a ``t3 notify send`` /
# ``t3 slack react``, or a ``git commit`` message. A non-forge,
# non-publish segment (``cd``, ``echo``, ``git push``) is inert -- it
# neither flips the command to user-facing nor counts as a forge post.
_T3_USER_FACING_SUBSTRINGS: Final[tuple[str, ...]] = ("notify send", "slack react")
_CURL_USER_FACING_MARKERS: Final[tuple[str, ...]] = ("chat.postmessage",)


class DestinationKind(StrEnum):
    EXTERNAL_FORGE = "external_forge"
    USER_FACING = "user_facing"


def _segment_is_forge_posting_verb(words: list[str]) -> bool:
    if len(words) < _FORGE_POSTING_WORD_COUNT:
        return False
    tool, sub, verb = words[0], words[1], words[2]
    if tool == "gh":
        return (sub, verb) in _GH_ELIGIBLE_VERBS
    if tool == "glab":
        return (sub, verb) in _GLAB_ELIGIBLE_VERBS
    return False


def _segment_is_t3_forge_wrapper(words: list[str]) -> bool:
    if not words or words[0] != "t3":
        return False
    joined = " ".join(words)
    return any(needle in joined for needle in _T3_FORGE_SUBSTRINGS)


def _segment_is_external_forge(words: list[str]) -> bool:
    rest = _strip_cd_prefix(words)
    return _segment_is_forge_posting_verb(rest) or segment_is_api_write(rest) or _segment_is_t3_forge_wrapper(rest)


def _segment_is_user_facing_publish(words: list[str]) -> bool:
    rest = _strip_cd_prefix(words)
    if not rest:
        return False
    if rest[0] == "git":
        return len(rest) >= 2 and rest[1] == "commit"  # noqa: PLR2004
    if rest[0] == "t3":
        joined = " ".join(rest)
        return any(needle in joined for needle in _T3_USER_FACING_SUBSTRINGS)
    joined_lower = " ".join(rest).lower()
    return any(marker in joined_lower for marker in _CURL_USER_FACING_MARKERS)


def classify_bash_destination(command: str) -> DestinationKind:
    """Classify a Bash publish ``command`` as external-forge or user-facing.

    EXTERNAL_FORGE only when at least one segment is an external-forge post
    and NO segment is a publish to anything else. A single non-forge or
    unclassifiable publish segment makes the whole command USER_FACING --
    the safer, gate-enforcing default.
    """
    segments = segment_word_lists(command)
    if not segments:
        return DestinationKind.USER_FACING
    saw_forge = False
    for words in segments:
        if _segment_is_external_forge(words):
            saw_forge = True
        elif _segment_is_user_facing_publish(words):
            return DestinationKind.USER_FACING
    return DestinationKind.EXTERNAL_FORGE if saw_forge else DestinationKind.USER_FACING
