"""File the push-gate selection-audit tracking ticket through the hygiene facade (#162).

The workflow step this replaces ran a bare ``gh issue create``, which answers none
of the questions :mod:`teatree.core.issue_hygiene` exists to answer: is there
already an open ticket for this miss, does the text belong in a description or a
comment, and has the outbound been through the public-repo leak gate. Because a
measured false negative recurs on every PR that trips the audit, the bare create
filed one duplicate per PR — a backlog of identical tickets nobody reads.

Marker-keyed, so a recurrence EXTENDS the open ticket with this PR's citation
instead of filing again. ``GITHUB_TOKEN`` is an installation token, so
``GET /user`` is not accessible to it and the Rule 5 self-identity set narrows to
empty: an extend is then refused as ``external_conflict``, which is the SAFE
degradation — the ticket already exists and is reported, and nothing is
duplicated. The first filing needs no identity, so the tracking ticket is always
created.

Never fatal. The audit step is ``continue-on-error``, and a tracking ticket that
could not be filed must not turn a measured false negative into a red herring
about issue filing — the audit job itself already failed loud.
"""

import os
import sys

_MARKER = "<!-- t3-ci:push-gate-selection-audit -->"
_TITLE = "push-gate selection-audit: measured false negative (#122)"
_LABEL = "needs-triage"


def build_body(*, pr_number: str, pr_url: str) -> str:
    """The tracking ticket's body, carrying the dedupe marker that makes a recurrence an append."""
    pr = f"#{pr_number}" if pr_number else "an unnumbered PR"
    return (
        f"{_MARKER}\n\n"
        f"The incremental push gate would have SKIPPED a whole-tree ast-grep finding or "
        f"doctest failure on PR {pr} ({pr_url or 'no URL reported'}). See the failed "
        f"selection-audit job logs for the offending path(s)."
    )


def file_audit_issue(*, repo: str, token: str, pr_number: str, pr_url: str) -> str:
    """File or extend the tracking ticket; return the human line describing what happened."""
    from teatree.backends.github.client import GitHubCodeHost
    from teatree.core.issue_hygiene import IssueDraft, create_or_extend_by_marker

    draft = IssueDraft(
        repo=repo,
        title=_TITLE,
        body=build_body(pr_number=pr_number, pr_url=pr_url),
        labels=(_LABEL,),
        action="ci_selection_audit",
    )
    outcome = create_or_extend_by_marker(host=GitHubCodeHost(token=token), draft=draft, marker=_MARKER)
    if outcome.kind == "external_conflict":
        return f"a matching tracking ticket already exists at {outcome.issue_url} — not duplicating it"
    return f"{outcome.kind}: {outcome.issue_url or '(no URL returned)'}"


def main() -> int:
    """Always 0: a tracking ticket is bookkeeping, and the audit job already failed loud."""
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        print("selection-audit filer: GITHUB_REPOSITORY and GH_TOKEN are both required — nothing filed")
        return 0

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django

    django.setup()

    try:
        print(
            "selection-audit filer: "
            + file_audit_issue(
                repo=repo,
                token=token,
                pr_number=os.environ.get("PR_NUMBER", ""),
                pr_url=os.environ.get("PR_URL", ""),
            )
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not mask the audit failure it reports on
        print(f"selection-audit filer: could not file the tracking ticket ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
