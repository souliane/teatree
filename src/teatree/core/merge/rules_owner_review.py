"""Head-bound owner approval for changes to the rules file."""

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import changed_paths_unavailable
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.review.review_taken import is_bot_author
from teatree.paths import VENDORED_CORE_PREFIX

_RULES_PATH = "skills/rules/RULES.md"


def assert_rules_owner_review(query: CodeHostQuery, *, expected_head: str) -> None:
    try:
        paths = query.pr_changed_paths()
    except Exception as exc:
        msg = "owner review guard could not read PR paths"
        raise MergePreconditionError(msg) from exc
    if not isinstance(paths, list) or not paths or changed_paths_unavailable(paths):
        msg = "owner review guard could not read PR paths"
        raise MergePreconditionError(msg)
    if not any(
        path.lstrip("/").removeprefix("./") in {_RULES_PATH, f"{VENDORED_CORE_PREFIX}{_RULES_PATH}"} for path in paths
    ):
        return
    try:
        live_head = query.live_head_sha()
        aliases = {alias.casefold() for alias in get_effective_settings().user_identity_aliases if alias}
        comments = query.backend.list_pr_comments(repo=query.ref.slug, pr_iid=query.ref.pr_id)
    except Exception as exc:
        msg = "owner review for rules change could not be verified"
        raise MergePreconditionError(msg) from exc
    if live_head != expected_head or not aliases or not isinstance(comments, list):
        msg = "owner review for current rules head is required"
        raise MergePreconditionError(msg)
    approval = f"Approve Rule -1 at {expected_head}"
    for comment in comments:
        if not isinstance(comment, dict) or comment.get("body") != approval or comment.get("system"):
            continue
        actor = comment.get("user") or comment.get("author")
        if not isinstance(actor, dict):
            continue
        name = actor.get("login") or actor.get("username")
        if (
            isinstance(name, str)
            and name.casefold() in aliases
            and not is_bot_author(name, user_type=actor.get("type", ""))
        ):
            return
    msg = f"owner review for current rules head is required: the owner comments exactly '{approval}'"
    raise MergePreconditionError(msg)
