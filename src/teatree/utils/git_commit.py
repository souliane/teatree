"""Commit, log, rev-list and message-parsing operations.

The commit/history partition of :mod:`teatree.utils.git`. Reads from the commit
graph (merge-base, rev-count, log, message extraction) and the two local
history mutators (soft-reset, commit), all via the
:mod:`teatree.utils.git_run` runners.
"""

from teatree.utils.git_run import run, run_strict


def merge_base(repo: str = ".", target: str = "origin/main") -> str:
    return run_strict(repo=repo, args=["merge-base", target, "HEAD"])


def branch_diff(repo: str = ".", target: str = "origin/main") -> str:
    """Diff of this branch's commits against their merge-base with *target*.

    Measures what the branch actually changes (``<merge-base>..HEAD``), so a
    per-diff gate sees the PR's committed lines and never the clone's unrelated
    uncommitted edits. Prefixes are forced (``a/``/``b/``) so the unified-diff
    parser is independent of a user's ``diff.noprefix`` config.
    """
    base = merge_base(repo, target)
    return run(repo=repo, args=["diff", base, "HEAD", "--src-prefix=a/", "--dst-prefix=b/"])


def rev_count(repo: str = ".", range_spec: str = "") -> int:
    out = run_strict(repo=repo, args=["rev-list", "--count", range_spec])
    return int(out)


def log_oneline(repo: str = ".", range_spec: str = "") -> str:
    return run(repo=repo, args=["log", "--oneline", range_spec])


def unsynced_commits(repo: str, branch: str, target: str = "origin/main") -> list[str]:
    output = run(repo=repo, args=["log", branch, "--not", target, "--oneline"])
    return [line for line in output.splitlines() if line.strip()]


def soft_reset(repo: str = ".", target: str = "") -> None:
    run_strict(repo=repo, args=["reset", "--soft", target])


def commit(repo: str = ".", message: str = "") -> None:
    run_strict(repo=repo, args=["commit", "-m", message])


def last_commit_message(repo: str = ".") -> tuple[str, str]:
    output = run(repo=repo, args=["log", "-1", "--format=%s%n%n%b"])
    lines = output.split("\n", 1)
    subject = lines[0].strip()
    body = lines[1].strip() if len(lines) > 1 else ""
    return subject, body


def first_commit_message(repo: str = ".", range_spec: str = "") -> tuple[str, str]:
    """Return ``(subject, body)`` of the OLDEST commit in *range_spec*.

    The canonical PR title for a branch is its first (oldest) own commit —
    the same commit a squash-merge keeps as the squash title. Unlike
    :func:`last_commit_message` (which reads ``HEAD`` and so can pick up the
    default branch's head when the wrong ref is checked out), this sources
    explicitly from a range like ``origin/main..my-branch`` and is therefore
    independent of the working tree. An empty/missing range, or a range with
    no commits, yields ``("", "")`` so the caller can fall back safely
    instead of mislabelling the PR.

    A ``%x1f`` unit separator splits subject from body and a ``%x1e`` record
    separator splits commits, so a body containing blank lines never bleeds
    into the next commit.
    """
    if not range_spec:
        return "", ""
    unit_sep, record_sep = "\x1f", "\x1e"
    output = run(repo=repo, args=["log", "--reverse", range_spec, f"--format=%s{unit_sep}%b{record_sep}"])
    records = [chunk for chunk in output.split(record_sep) if chunk.strip()]
    if not records:
        return "", ""
    subject, _, body = records[0].lstrip("\n").partition(unit_sep)
    return subject.strip(), body.strip()


def commit_messages(repo: str = ".", range_spec: str = "") -> list[str]:
    """Return the full message (subject + body) of each commit in *range_spec*.

    Commits are separated by an ASCII record separator so a body that
    itself contains blank lines never splits one commit into two. An
    empty/missing range yields ``[]`` (nothing to scan, not an error) —
    a close-keyword gate over zero commits must pass, not blow up.
    """
    if not range_spec:
        return []
    sep = "\x1e"
    output = run(repo=repo, args=["log", range_spec, f"--format=%B{sep}"])
    return [chunk.strip() for chunk in output.split(sep) if chunk.strip()]
