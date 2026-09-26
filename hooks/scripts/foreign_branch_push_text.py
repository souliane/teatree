"""Every refusal the foreign-branch push gate emits, assembled in one place.

Split out of the gate itself: that module was at the module-health LOC cap with
the ownership ladder, the shell parsing and the probes all in it, and refusal
PROSE is the seam that shares nothing with the decision above it. It takes
primitives rather than a ``PushSpec`` so nothing here imports the gate back.

Every untrusted operand is CLIPPED at its interpolation, so a 60 KB one cannot
take the cause, the remedy and the instruction for turning this gate off down
with it. `bounded` redacts as well — right for an operand arriving raw, wrong
for a probe's cause, where a scrub partitions git's own `Expected git repo
version <= 1` into `<=<redacted>` in the body that quotes it. Redacting that one
is the ASSEMBLED text's job at the bottom of :func:`refusal`, which stays as the
structural backstop — a clause added later cannot reintroduce a leak by
forgetting to scrub — and is idempotent over text already scrubbed.

It is not, and cannot be, the bound: that scrub TRUNCATES at the scanner's cap,
so one unbounded operand takes the cause, the remedy and the instruction for
turning this gate off down with it, leaving a fail-closed refusal that names no
way forward. Measured: a 100000-character refspec left 176 characters.
"""

from typing import Final

from hooks.scripts.credential_redaction import clip, redact_credentials
from hooks.scripts.foreign_branch_push_git import _MAX_QUOTED_CHARS, GitProbe, bounded, probe_cause

# The branch label a refusal carries when the push pinned no single branch:
# no ownership was established, so the ownership remedy does not apply.
UNPINNED: Final[str] = "<unpinned>"
# The only two ways git reports no repository; any other non-zero cause leaves
# the question open, so the refusal may not answer it. ENGLISH git only: these
# strings are gettext-translated upstream, so a localised git matches neither and
# falls to the conservative "could not determine" branch. Both branches REFUSE, so
# safety is unaffected and only the message's specificity degrades.
_ABSENT_REPOSITORY_STDERR: Final[tuple[str, str]] = ("not a git repository", "does not appear to be a git repository")
_NO_OVERRIDE: Final[str] = (
    "There is no per-call override for this gate; the owner turns it off deliberately with "
    "`t3 <overlay> gate foreign-push disable`."
)
_UNREAD_SUBJECTS: Final[dict[str, str]] = {
    "wrapper": "the `{leader}` wrapper",
    "shell-payload": "the `{leader}` wrapper's script (there is no literal `-c` payload to read)",
    "shell-heredoc": "a heredoc executed by `{leader}`",
    "nesting": "wrappers nested deeper than this gate reads",
    "unbalanced-quoting": "unbalanced quoting",
}


def refusal(remote: str, branch: str, body: str, *, force: bool) -> str:
    """The whole refusal for one push, assembled and redacted in ONE place.

    *remote* is a push's first operand, which git accepts as a full URL: a
    credential in its userinfo or query string reaches the headline, the probe
    argv and the remedy without ever passing through a probe. *body* arrives
    already bounded from the builder that assembled it, for the second reason
    `bounded` exists — see this module's own docstring.
    """
    forced = " --force" if force else ""
    return redact_credentials(
        f"REFUSED: `git push{forced} {bounded(remote)} {bounded(branch)}` is a write to a branch this agent "
        f"has not established as ours. {body} A 28-file commit and then a FORCE-PUSH landed on a "
        f"colleague's open draft MR this way, rewriting their history unasked. {_remedy(branch)} {_NO_OVERRIDE}"
    )


def _remedy(branch: str) -> str:
    """What to do instead — the OWNERSHIP answer, or the re-issue one for an unpinned target.

    A refusal for an unpinned target established nothing about ownership, so
    naming a person to hand the findings back to points at nobody and buries
    the one line that matters: the body above already said what went unresolved.
    """
    if branch == UNPINNED:
        return "Re-issue the push so it names both its repository and one branch, and this gate can answer."
    return (
        f"Do this instead: cut our OWN branch off the freshly-fetched default "
        f"(`t3 <overlay> workspace ticket <issue>`) and open our OWN MR against it, or hand the "
        f"findings back to whoever owns `{bounded(branch)}` rather than pushing."
    )


def default_branch_rewrite_refusal(remote: str, branch: str) -> str:
    """Refuse a FORCE or DELETE onto the remote's default branch, whoever owns it."""
    return redact_credentials(
        f"REFUSED: this push rewrites or deletes `{bounded(branch)}` on `{bounded(remote)}`, the remote's "
        f"DEFAULT branch, the one every other branch is cut from. Ownership does not enter into it — a "
        f"shared history is nobody's to rewrite unasked. Push the work as its OWN branch and open an MR "
        f"(`t3 <overlay> workspace ticket <issue>`), or re-issue without `--force`/`--delete`. {_NO_OVERRIDE}"
    )


def unread_refusal(kind: str, leader: str) -> str:
    """Refuse material naming a push this gate could not read, naming the way through."""
    subject = _UNREAD_SUBJECTS[kind].format(leader=bounded(leader))
    return redact_credentials(
        f"REFUSED: this gate could not read a git push inside {subject}, and a push it cannot read is "
        f"not a push it may allow. Re-issue the push as ONE single-line Bash call — "
        f"`git push <remote> <branch>` with no wrapper (`sudo -u`, `nice -n`, `env -i`, `xargs`, `eval`, "
        f"`ssh`), no heredoc, no line continuation, nothing else on the line — so it can be judged, and "
        f"keep the rest of the script in a separate call. {_NO_OVERRIDE}"
    )


def unowned_branch_body(remote: str, branch: str, base_branch: str) -> str:
    return (
        f"`{bounded(branch)}` already exists on `{bounded(remote)}` with no open MR and no commit beyond "
        f"`{bounded(base_branch)}`, so nothing on it says whose it is. If it is yours, open your MR on it "
        f"first (`t3 <overlay> pr ensure-pr --repo <repo> --branch {bounded(branch)}`) or cut a fresh branch."
    )


def foreign_mr_body(author: str, mr_ref: str) -> str:
    return f"The open MR/PR on it, {bounded(mr_ref)}, is authored by `{bounded(author)}` — not by us."


def foreign_commits_body(branch: str, authors: tuple[str, ...]) -> str:
    who = bounded(", ".join(f"`{name}`" for name in authors))
    return (
        f"No MR backs `{bounded(branch)}`, but every commit it carries beyond the default branch is "
        f"authored by {who} and none by us."
    )


def unobtainable_body(probe: str, result: GitProbe | None = None) -> str:
    cause = f" It failed with {clip(probe_cause(result), _MAX_QUOTED_CHARS)}." if result is not None else ""
    return (
        f"Ownership could not be established: {bounded(probe)} did not answer, so whether this branch "
        f"belongs to someone else is UNKNOWN.{cause} This gate fails closed on an unanswered probe."
    )


def no_repository_body(work_dir: str, remote: str, target: str, repo: GitProbe) -> str:
    """The refusal for a ``work_dir`` git did not confirm holds a repository.

    Only git SAYING SO establishes absence: an unsupported repository version
    and dubious ownership exit non-zero over a directory that IS one, and a
    directory git could not enter — like one a probe never ran against — was
    never looked inside. Claiming absence there contradicts the cause quoted
    two clauses later, and the ``git -C <repo>`` remedy reproduces that cause.
    """
    where, whom, what = bounded(work_dir), bounded(remote), bounded(target)
    if not _stderr_reports_no_repository(repo):
        why = "The probe never ran" if repo.code is None else "Git failed without reporting one absent"
        return (
            f"Ownership could not be established: this hook could not determine whether "
            f"`{where}` is a git repository, so it could not ask `{whom}` about "
            f"`{what}`. {why}: {clip(probe_cause(repo), _MAX_QUOTED_CHARS)}. This gate fails closed on a "
            f"probe that could not answer."
        )
    return (
        f"Ownership could not be established: there is no git repository at `{where}`, the "
        f"directory this hook reports the command running in, so `{whom}` names no remote to "
        f"ask about `{what}`. Git said: "
        f"{clip(probe_cause(repo), _MAX_QUOTED_CHARS)}. This gate fails closed rather than guess which repository "
        f"the push writes. Make it answerable by naming that repository on the push itself — "
        f"`git -C <repo> push {whom} <branch>`, or `cd <repo> && git push {whom} "
        f"<branch>` — and this gate probes the same repository the push writes."
    )


def _stderr_reports_no_repository(repo: GitProbe) -> bool:
    """Whether git ITSELF reported no repository, rather than merely exiting non-zero."""
    return repo.code is not None and any(phrase in repo.err.lower() for phrase in _ABSENT_REPOSITORY_STDERR)
