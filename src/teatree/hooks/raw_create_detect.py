"""Action-aware detection of a raw forge MR/PR-create invocation.

The CREATE sibling of :mod:`teatree.hooks.raw_merge_detect`. A repo may declare that its
MRs are authored under a non-owner credential, so the human owner stays eligible to approve
them (:meth:`~teatree.core.overlay.OverlayConfig.get_gitlab_token_for_remote`). A raw ``gh pr
create`` / ``glab mr create`` never consults that declaration — the forge CLI writes under
whatever credential IT is configured with, which is the owner's — and a forge bars an author
from approving their own MR. The MR is born unapprovable and nothing says so until somebody
tries to approve it, hours later, against an HTTP 401 that reads like a dead token.

Shape detection only: whether a command IS a raw create invocation. Whether the TARGET repo
requires a non-owner author is a separate, stateful question the router-level gate asks. This
module stays a stdlib-only leaf (plus the shared :mod:`teatree.hooks.forge_subcommand`
traversal), importable by Lane B and the cold PreToolUse subprocess alike.
"""

import re
from urllib.parse import unquote

from teatree.hooks.forge_subcommand import ApiCall, forge_api_calls, forge_subcommand_argvs, invokes_forge_subcommand

# The forge programs and the create subcommand words that follow.
_CREATE_SUBWORDS: dict[str, tuple[str, ...]] = {"gh": ("pr", "create"), "glab": ("mr", "create")}

# The REST create-COLLECTION endpoint: ``merge_requests`` / ``pulls`` NOT immediately followed
# by ``/<iid>`` — a numbered endpoint is an existing MR's own resource (an update, not a
# create), and a nested one (``/merge_requests/42/notes``) is a sub-resource write.
_CREATE_ENDPOINT_RE = re.compile(r"\b(?:merge_requests|pulls)\b(?!/\d)")

# The repo an API endpoint names: ``repos/<owner>/<repo>/pulls`` or ``projects/<ns>/merge_requests``.
_API_TARGET_RE = re.compile(r"(?:^|/)repos/(?P<gh>[^/]+/[^/?]+)/pulls|(?:^|/)projects/(?P<gl>[^/?]+)/merge_requests")

_RAW_CREATE_DENY_REASON = (
    "raw `gh pr create` / `glab mr create` opens an MR under whichever credential the forge CLI "
    "is configured with — on a repo declaring a non-owner authoring credential that is the OWNER's, "
    "producing an MR its own author is barred from approving (the forge answers that refusal with "
    "HTTP 401, which reads as a dead token). Use `t3 <overlay> pr create <ticket-id>`, or "
    "`t3 <overlay> pr ensure-pr --repo <abs-path> --branch <branch>` when the branch has no ticket."
)


def invokes_raw_create_subcommand(command: str) -> bool:
    """Whether *command* INVOKES ``gh pr create`` / ``glab mr create`` as an executed program.

    Errs toward BLOCK: fires on any plausible invocation (env-prefixed, wrapper-prefixed,
    path-qualified, grouped/compound, or inside a command substitution). Only a heredoc body,
    a ``#`` comment, and a quoted-string operand pass through.
    """
    return invokes_forge_subcommand(command, _CREATE_SUBWORDS)


def is_raw_create_api_write(command: str) -> bool:
    """Whether *command* EXECUTES a raw forge REST WRITE to an MR/PR COLLECTION endpoint.

    True only when an executed ``gh``/``glab api`` call targets ``.../merge_requests`` or
    ``.../pulls`` with NO trailing ``/<iid>`` (the collection — a create) AND its effective HTTP
    method is not GET. A GET reads the list; a write to a NUMBERED endpoint edits an EXISTING
    MR. Neither is a create, and an API call that is only quoted or described is not executed.
    """
    return any(_is_create_call(call) for call in forge_api_calls(command))


def raw_create_targets(command: str) -> list[str | None]:
    """The target of each raw create *command* EXECUTES, read from that invocation's own argv.

    ``None`` stands for an invocation that names no target, so text elsewhere in the command —
    a quoted example, another invocation's ``--repo`` — can never lend it one.
    """
    targets = [_cli_target(argv) for argv in forge_subcommand_argvs(command, _CREATE_SUBWORDS)]
    targets += [_api_target(call.endpoint or "") for call in forge_api_calls(command) if _is_create_call(call)]
    return targets


def _is_create_call(call: ApiCall) -> bool:
    return call.is_write and call.endpoint is not None and bool(_CREATE_ENDPOINT_RE.search(call.endpoint))


def _cli_target(argv: list[str]) -> str | None:
    words = iter(argv)
    for word in words:
        if word in {"-R", "--repo"}:
            return next(words, None)
        if word.startswith("--repo="):
            return word.removeprefix("--repo=")
        if word.startswith("-R"):
            return word[2:].removeprefix("=")
    return None


def _api_target(endpoint: str) -> str | None:
    match = _API_TARGET_RE.search(endpoint)
    if match is None:
        return None
    return match.group("gh") or unquote(match.group("gl"))


def raw_create_deny_reason(command: str) -> str | None:
    """The raw-create SHAPE deny reason for *command*, or ``None`` when it is not one.

    A SHAPE test: whether this command is a raw MR/PR create at all, via the literal subcommand
    (action-aware) or the REST collection-write vector. Whether the target repo actually
    requires a non-owner author is asked by the router-level gate, which owns the stateful half.
    """
    if not command:
        return None
    if invokes_raw_create_subcommand(command) or is_raw_create_api_write(command):
        return _RAW_CREATE_DENY_REASON
    return None


__all__ = [
    "invokes_raw_create_subcommand",
    "is_raw_create_api_write",
    "raw_create_deny_reason",
    "raw_create_targets",
]
