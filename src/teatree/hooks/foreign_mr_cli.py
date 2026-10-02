"""Shell seam for the foreign-open-MR pre-push guard.

The guard refuses a push to a branch that backs an OPEN MR/PR authored by
someone else — pushing there silently rewrites a colleague's MR. It resolved
that MR with ``gh`` alone and exited early when ``gh`` was absent, so on a
GitLab remote it could never fire at all: the one forge it knew how to ask was
the one the branch does not live on, and a guard that cannot fire reads exactly
like a guard that found nothing.

This CLI is the thin seam onto the host-keyed routing
:mod:`teatree.hooks._forge_tool` owns, mirroring
:mod:`teatree.hooks.repo_visibility_cli`. It is deliberately Django-free — the
import chain is ``utils.run`` and the ``_repo_visibility`` slug normaliser — so
a pre-push hook pays an interpreter start, not a framework boot.

Each forge is asked in its OWN idiom: ``gh`` carries a built-in ``--jq``, while
``glab api`` has no such flag (passing one makes it exit non-zero on an unknown
flag), so the GitLab side parses JSON here.

Usage::

    python -m teatree.hooks.foreign_mr_cli <remote-url-or-slug> <branch>

Prints exactly one line and exits 0 whenever it could run:

``NONE``
    no open MR backs the branch, OR the guard never got as far as asking who
    owns one — an unparsable remote, an unrecognised host, an absent forge CLI,
    a failed MR query. The caller lets the push through.
``UNKNOWN <number> <author> <tool> <cause>``
    an open MR backs the branch and the pushing identity could NOT be resolved,
    so OWN-vs-FOREIGN has no ground to be decided on. The caller REFUSES: an
    unresolvable identity is not evidence the push is harmless, and answering
    NONE here made the guard's protection depend on which venue it ran in. The
    ``cause`` is what the probe was OBSERVED to do (a ``PROBE_*`` token) — a
    timeout and an unauthenticated CLI have different remedies, so the refusal
    reports the one it saw instead of asserting the credential is at fault.
``OWN <number>``
    the open MR is the configured identity's own, or one of the logins the
    operator declared for that host in ``self_forge_identities`` — a bot that
    authors our MRs so we stay eligible to approve them.
``FOREIGN <number> <author> <us>``
    a CONFIRMED foreign open MR — the only verdict the caller blocks on.

Field 3 is the MR author on both verdicts the caller blocks on, so the shell
reads the ``<kind> <number> <author>`` prefix once and only the tail per kind.
"""

import json
import sys
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from teatree.config import cold_reader
from teatree.hooks._forge_tool import FORGE_TOOL, GITHUB, GITLAB, forge_and_repo_path, host_of_slug
from teatree.hooks._repo_visibility import ForgeProbe, run_forge_tool, slug_for_remote_url

NONE_VERDICT: Final[str] = "NONE"
UNKNOWN_VERDICT: Final[str] = "UNKNOWN"

#: Host-keyed logins the operator ALSO acts as — its own bots, never a teammate.
SELF_IDENTITIES_SETTING: Final[str] = "self_forge_identities"

#: The probe ran and exited 0, but its payload named no login.
PROBE_NO_LOGIN: Final[str] = "no-login-in-payload"


@dataclass(frozen=True, slots=True)
class OpenMr:
    """The open MR/PR backing a branch, and who authored it."""

    number: str
    author: str


@dataclass(frozen=True, slots=True)
class Identity:
    """Who this venue is on a forge, or the cause the probe could not say.

    An empty ``login`` always carries a non-empty ``unresolved`` — the refusal
    downstream names what was observed, and it can only do that if the outcomes
    reach it apart.
    """

    login: str = ""
    unresolved: str = ""


def _first_json_object(stdout: str) -> dict | None:
    """The first mapping of a JSON array payload, or ``None`` for anything else."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    return payload[0]


def _identity_of(probe: ForgeProbe, login: str) -> Identity:
    if login:
        return Identity(login=login)
    return Identity(unresolved=probe.unresolved or PROBE_NO_LOGIN)


def _github_identity() -> Identity:
    probe = run_forge_tool(FORGE_TOOL[GITHUB], ["api", "user", "--jq", ".login"])
    return _identity_of(probe, (probe.stdout or "").strip())


def _github_open_pr(repo_path: str, branch: str) -> OpenMr | None:
    """The open PR whose head branch is exactly *branch*, via ``gh``'s own ``--jq``."""
    stdout = run_forge_tool(
        FORGE_TOOL[GITHUB],
        [
            "pr",
            "list",
            "--repo",
            repo_path,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,author",
            "--jq",
            r'.[] | "\(.number)\t\(.author.login)"',
        ],
    ).stdout
    if not stdout:
        return None
    number, _tab, author = stdout.splitlines()[0].partition("\t")
    return OpenMr(number=number.strip(), author=author.strip()) if author.strip() else None


def _gitlab_identity() -> Identity:
    probe = run_forge_tool(FORGE_TOOL[GITLAB], ["api", "user"])
    return _identity_of(probe, _username_of(probe.stdout))


def _username_of(stdout: str | None) -> str:
    """The ``username`` a ``glab api user`` payload names, or ``""``."""
    try:
        user = json.loads(stdout or "")
    except ValueError:
        return ""
    username = user.get("username") if isinstance(user, dict) else None
    return username.strip() if isinstance(username, str) else ""


def _gitlab_open_mr(repo_path: str, branch: str) -> OpenMr | None:
    """The open MR whose source branch is exactly *branch*, parsed from ``glab api`` JSON."""
    project = quote(repo_path, safe="")
    query = f"projects/{project}/merge_requests?source_branch={quote(branch, safe='')}&state=opened"
    stdout = run_forge_tool(FORGE_TOOL[GITLAB], ["api", query]).stdout
    if stdout is None:
        return None
    merge_request = _first_json_object(stdout)
    if merge_request is None:
        return None
    author = merge_request.get("author")
    username = author.get("username") if isinstance(author, dict) else None
    if not isinstance(username, str) or not username.strip():
        return None
    return OpenMr(number=str(merge_request.get("iid", "")), author=username.strip())


def declared_self_identities(host: str) -> frozenset[str]:
    """Logins the operator declared as its own on *host*, lowercased.

    A forge CLI answers with ONE login, so an MR authored by our own bot reads
    exactly like a teammate's. Declaring nothing keeps the verdict unchanged.
    """
    if not host:
        return frozenset()
    declared = cold_reader.mapping_setting(SELF_IDENTITIES_SETTING).get(host)
    if not isinstance(declared, list):
        return frozenset()
    return frozenset(e.strip().lower() for e in declared if isinstance(e, str) and e.strip())


def foreign_mr_verdict(remote: str, branch: str) -> str:
    """Return the one-line verdict for *branch* on *remote* (see the module docstring).

    Fail-open up to the point an open MR is found: an unparsable remote, an
    unrouted host, an absent branch and a failed MR query all collapse to
    :data:`NONE_VERDICT`, so a venue that cannot reach the forge at all still
    pushes. Once an MR IS found the question is live, and an identity neither
    the declaration nor the probe can settle yields :data:`UNKNOWN_VERDICT`
    rather than silently allowing it.
    """
    slug = slug_for_remote_url(remote.strip())
    forge, repo_path = forge_and_repo_path(slug)
    if not forge or not branch.strip():
        return NONE_VERDICT
    open_mr = _github_open_pr(repo_path, branch) if forge == GITHUB else _gitlab_open_mr(repo_path, branch)
    if open_mr is None:
        return NONE_VERDICT
    author = open_mr.author.lower()
    # A cold config read, so it answers in the very venues the probe cannot;
    # asking it only AFTER the probe refused pushes to MRs already declared ours.
    if author in declared_self_identities(host_of_slug(slug)):
        return f"OWN {open_mr.number}"
    identity = _github_identity() if forge == GITHUB else _gitlab_identity()
    if not identity.login:
        return f"{UNKNOWN_VERDICT} {open_mr.number} {open_mr.author} {FORGE_TOOL[forge]} {identity.unresolved}"
    if author == identity.login.lower():
        return f"OWN {open_mr.number}"
    return f"FOREIGN {open_mr.number} {open_mr.author} {identity.login}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    expected_args = 2
    if len(args) != expected_args or not args[0].strip():
        sys.stdout.write(f"{NONE_VERDICT}\n")
        return 0
    sys.stdout.write(f"{foreign_mr_verdict(args[0], args[1])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
