"""Stranger-PR policy — ignore-until-admitted, fail-closed (#3634 section 4).

The factory does not look at an untrusted author's PR until the owner applies the
admit label. That closes the prompt-injection surface an unattended reviewer would
otherwise expose to arbitrary PR text, and conserves review cycles. Once admitted
the PR IS reviewed — but merge authority is untouched: an untrusted author's PR is
never auto-merged, whatever the label says.

Admission runs through the SAME decision function as issue intake
(:func:`~teatree.core.intake.factory_admission.decide_intake`), so "who may the
factory work for" has exactly one answer. Author trust uses the same strict
conjunction intake uses — the shared classifier AND explicit trusted-set
membership — so the classifier's private-repo bypass cannot hand an unlisted
collaborator the reviewer.
"""

from typing import cast
from urllib.parse import urlparse

from teatree.core.intake.factory_admission import IntakeFacts, decide_intake, payload_labels
from teatree.core.review.author_trust import classify_author, is_trusted_author
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slug_from_issue_or_pr_url


def _pr_author(pr: RawAPIDict) -> str:
    """The handle that OPENED *pr*, across GitHub (``user.login``) and GitLab (``author.username``)."""
    for container in ("user", "author"):
        node = pr.get(container)
        if not isinstance(node, dict):
            continue
        actor = cast("RawAPIDict", node)
        for name in ("login", "username"):
            handle = actor.get(name)
            if isinstance(handle, str) and handle.strip():
                return handle.strip()
    return ""


def pr_is_admitted(pr: RawAPIDict, *, pr_url: str, trusted: frozenset[str], admit_label: str) -> bool:
    """Whether the factory may review *pr*.

    Fail-closed: an unresolvable author, an unparsable PR URL, and an unset admit
    label all resolve to "not admitted", never to "review it".
    """
    author = _pr_author(pr)
    parsed = urlparse(pr_url)
    slug = slug_from_issue_or_pr_url(parsed.path)
    if not author or not slug:
        return False
    host_kind = "gitlab" if "/-/" in parsed.path or "gitlab" in (parsed.hostname or "").lower() else "github"
    classification = classify_author(slug, author, host_kind=host_kind, extra_trusted=trusted)
    author_trusted = classification.trusted and is_trusted_author(author, extra_trusted=trusted)
    verdict = decide_intake(
        IntakeFacts(labels=payload_labels(pr), work_exists=False, author_trusted=author_trusted),
        admit_label=admit_label,
    )
    return verdict.acts
