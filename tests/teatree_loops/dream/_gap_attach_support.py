import hashlib
from unittest.mock import MagicMock

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.gates.fold_preservation import fold_body, fold_marker
from teatree.core.models import ConsolidatedMemory, Ticket
from tests.teatree_loops.dream._own_umbrella import claims_self, ours

UMBRELLA = "https://gitlab.com/o/factory/-/work_items/249"
HOST_URL = "https://gitlab.com/o/factory/-/work_items/56"


def _section_marker(body: str) -> str:
    return next(line for line in body.splitlines() if line.startswith("<!-- t3-hygiene:"))


def _forge(*, body: str = "## Gates that cost a day\n", persists: bool = True, author: str = "") -> CodeHostBackend:
    state = {"body": body}

    def _update(**kwargs: object) -> dict[str, int]:
        if persists:
            state["body"] = str(kwargs["body"])
        return {"iid": 56}

    def _issue(*_a: object, **_k: object) -> dict[str, object]:
        issue = ours({"description": state["body"]})
        return {**issue, "user": {"login": author}} if author else issue

    forge = claims_self(MagicMock(spec=CodeHostBackend))
    forge.get_issue.side_effect = _issue
    forge.update_issue.side_effect = _update
    forge.repo_for_issue_url.return_value = "o/factory"
    return forge


def _umbrella(*keys: str) -> Ticket:
    for key in keys:
        ConsolidatedMemory.objects.create(
            cluster_key=key,
            rule=f"Run the lane before pushing ({key}).",
            source_files=[f"feedback_{key}.md"],
            member_count=1,
            max_member_weight=90,
            verified_citation=f"task for {key} went red",
        )
    return Ticket.objects.create(
        issue_url=UMBRELLA,
        extra={
            "dream_gap_pending": [
                {"gap_key": key, "cluster_key": key, "title": f"Fix {key}", "citation": "cited"} for key in keys
            ]
        },
    )


def _host(state: str = Ticket.State.NOT_STARTED) -> Ticket:
    return Ticket.objects.create(issue_url=HOST_URL, role=Ticket.Role.AUTHOR, state=state)


def _current_fold(*keys: str) -> str:
    section = ""
    for key in keys:
        member = (
            f"Dream gap `{key}` — citation: cited.\n\n"
            f"Run the lane before pushing ({key}).\n\nCited: task for {key} went red"
        )
        section = fold_body(
            host_body=section, member_ref=f"dream-gap {key}", member_title=f"Fix {key}", member_body=member
        )
    digest = hashlib.sha256("\x1f".join(fold_marker(f"dream-gap {key}") for key in keys).encode()).hexdigest()[:16]
    return (
        f"## Gates that cost a day\n\n<!-- t3-hygiene:{digest} -->\n\n"
        f"## Dream gaps folded in (2026-10-01)\n\n{section.rstrip()}\n\n"
        f"<!-- t3-dream-gap-fold-end:{digest} -->"
    )
