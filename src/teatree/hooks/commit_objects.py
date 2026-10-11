"""The commit and tag objects a push publishes, read as the remote will hold them.

A push publishes more text than its patch: a commit's extra headers and its author and committer names, an annotated
tag's own lines, and a message exactly as the object stores it (the ``encoding`` header travels with the commit, so
``i18n.commitEncoding=UTF-16LE`` on plain bytes makes a decoded log show noise while the remote holds the message).
No CI job reads objects, so the gate does. This module is the one reader of them: the pre-push hook runs it with
``python -m`` from the copied committed sources, ``t3 fast-push`` imports it, and ``scripts/privacy_scan.py`` shares
its decoder.

The noreply cut is the one place that names the only email domain the identity rule allows: the email detector has
nothing to find in it, while the login in front of it stays visible to every detector (a token such as ``sk-…`` fits
the login shape).

``python -m teatree.hooks.commit_objects messages <rev-list args>`` prints each message as git decodes it, then as the
object stores it; ``commit|tag raw|blank <rev-list args | tag sha>`` prints the whole objects as they are, or with the
noreply cut. A read git could not complete exits 1 and prints nothing, because a scan that did not happen shows nothing.
"""

import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from teatree.utils.run import spawn_byte_pipe

__all__ = [
    "READ_FLAGS",
    "commit_objects",
    "decode_robust",
    "decoded_messages",
    "stored_message",
    "tag_object",
    "without_the_noreply_domain",
]

#: Repo-local git state (a textconv driver, an external diff) rewrites the text of a diff; the gates read the raw one.
READ_FLAGS = ("--no-textconv", "--no-ext-diff", "--no-color")

_NOREPLY_IDENTITY = re.compile(rb"^( ?(?:author|committer|tagger) .*)@users\.noreply\.github\.com>", re.MULTILINE)


def decode_robust(raw: bytes) -> str:
    """Decode *raw* as text, tolerating non-UTF-8 bytes rather than crashing.

    A git commit message can carry the author's configured ``i18n.commitEncoding`` /
    ``i18n.logOutputEncoding`` (e.g. ISO-8859-1), so the bytes are not guaranteed UTF-8. A raised
    ``UnicodeDecodeError`` would end the scan without a verdict on exactly the content a leak needs blocked.
    Latin-1 never raises (every byte maps to one codepoint), so failing back to it keeps ASCII secrets intact
    and scannable.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def without_the_noreply_domain(raw_object: bytes) -> bytes:
    """*raw_object* with the exact ``@users.noreply.github.com`` cut from its identity header lines.

    The indented copies inside a ``mergetag`` block are covered; the headers end at the first blank line, and a
    message is left as it is.
    """
    headers, separator, message = raw_object.partition(b"\n\n")
    return _NOREPLY_IDENTITY.sub(rb"\1>", headers) + separator + message


def stored_message(raw_object: bytes) -> bytes:
    """The message of a commit object exactly as it is stored: everything after the headers."""
    return raw_object.partition(b"\n\n")[2]


def _git(repo: Path | None, *args: str) -> bytes | None:
    """Stdout of ``git *args`` read unreplaced, in *repo* (the working directory when ``None``); ``None`` on failure."""
    pipe = spawn_byte_pipe(["git", "--no-replace-objects", *(["-C", str(repo)] if repo is not None else []), *args])
    try:
        pipe.stdin.close()
        data = pipe.stdout.read()
    finally:
        pipe.stdout.close()
        code = pipe.process.wait()
    return data if code == 0 else None


def commit_objects(rev_args: Sequence[str], repo: Path | None = None) -> list[bytes] | None:
    """Each commit of ``git rev-list *rev_args`` exactly as the object stores it, headers and message."""
    listing = _git(repo, "rev-list", *rev_args)
    if listing is None:
        return None
    objects: list[bytes] = []
    for sha in listing.decode("ascii", errors="replace").split():
        data = _git(repo, "cat-file", "commit", sha)
        if data is None:
            return None
        objects.append(data)
    return objects


def tag_object(sha: str, repo: Path | None = None) -> bytes | None:
    """The annotated tag object *sha* as it is stored."""
    return _git(repo, "cat-file", "-p", sha)


def decoded_messages(rev_args: Sequence[str], repo: Path | None = None) -> bytes | None:
    """The messages of ``git log *rev_args`` as git decodes them into UTF-8."""
    return _git(repo, "log", "--encoding=UTF-8", *READ_FLAGS, "--format=%B", *rev_args)


def _view(raw_object: bytes, how: Literal["raw", "blank"]) -> bytes:
    return without_the_noreply_domain(raw_object) if how == "blank" else raw_object


def _rendered(args: Sequence[str]) -> bytes | None:
    """The bytes the hook asked for, or ``None`` when git could not read them or the request is not one."""
    match list(args):
        case ["messages", *rev_args]:
            decoded, objects = decoded_messages(rev_args), commit_objects(rev_args)
            if decoded is None or objects is None:
                return None
            return decoded + b"".join(stored_message(obj) for obj in objects)
        case ["commit", ("raw" | "blank") as how, _, *_] as request:
            commits = commit_objects(request[2:])
            return None if commits is None else b"".join(_view(obj, how) for obj in commits)
        case ["tag", ("raw" | "blank") as how, sha]:
            tag = tag_object(sha)
            return None if tag is None else _view(tag, how)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Print the view *argv* names (see the module docstring); exit 1 printing nothing when it could not be read."""
    rendered = _rendered(sys.argv[1:] if argv is None else argv)
    if rendered is None:
        sys.stderr.write("commit_objects: could not read what was asked for (see the module docstring for the forms)\n")
        return 1
    sys.stdout.buffer.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
