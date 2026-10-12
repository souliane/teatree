"""The GitHub reads behind a merge-void content probe: one GraphQL query, compares, and recursive trees.

Every failure is UNREADABLE, never absent: a GraphQL ``errors`` payload, an HTTP 403 or
429, a truncated tree and a default branch that moves mid-probe all refuse the void.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.forge_content import PrContentSnapshot
from teatree.types import RawAPIDict

GhRun = Callable[[list[str]], tuple[int, str, str]]

_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
    repository(owner: $owner, name: $name) {
        defaultBranchRef { name target { oid } }
        pullRequest(number: $number) {
            merged mergeCommit { oid } headRefOid baseRefOid
            timelineItems(last: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, BASE_REF_FORCE_PUSHED_EVENT]) {
                nodes {
                    __typename
                    ... on HeadRefForcePushedEvent { createdAt beforeCommit { oid } afterCommit { oid } }
                    ... on BaseRefForcePushedEvent { createdAt beforeCommit { oid } afterCommit { oid } }
                }
            }
        }
    }
}
"""
_NO_COMMON_ANCESTOR = "No common ancestor"
_ANCESTOR_STATUSES = frozenset({"behind", "identical"})
_DIVERGED_STATUSES = frozenset({"ahead", "diverged"})


class _UnreadableError(Exception):
    pass


def _at(payload: object, *keys: str) -> object:
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload


def _text(payload: object, *keys: str) -> str:
    value = _at(payload, *keys)
    return value if isinstance(value, str) else ""


def _parsed(rc: int, out: str, err: str, *, what: str) -> RawAPIDict:
    if rc != 0:
        msg = f"{what} failed: {(err or out).strip()[:300]}"
        raise _UnreadableError(msg)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        msg = f"{what} returned unparsable JSON"
        raise _UnreadableError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{what} returned no JSON object"
        raise _UnreadableError(msg)
    return payload


@dataclass(frozen=True, slots=True)
class _ForcePush:
    kind: str
    created_at: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class _Pull:
    merged: bool
    merge_commit: str
    head: str
    base: str
    default_branch: str
    default_tip: str
    force_pushes: tuple[_ForcePush, ...]

    def earlier(self, kind: str, after: str) -> str:
        """The commit a force-push of *kind* replaced with *after* — the newest such push."""
        pushes = [push for push in self.force_pushes if push.kind == kind and push.after == after and push.before]
        return max(pushes, key=lambda push: push.created_at).before if pushes else ""


class GhPrContentReader:
    def __init__(self, run: GhRun) -> None:
        self._run = run

    def snapshot(self, *, slug: str, pr_id: int) -> PrContentSnapshot:
        try:
            return self._snapshot(slug, pr_id)
        except _UnreadableError as exc:
            return PrContentSnapshot(unreadable=str(exc))

    def _snapshot(self, slug: str, pr_id: int) -> PrContentSnapshot:
        pull = self._pull(slug, pr_id)
        if not pull.merged:
            return PrContentSnapshot(merged=False, default_branch=pull.default_branch, default_tip=pull.default_tip)
        if not pull.merge_commit:
            msg = "the forge reports the PR merged but names no merge commit"
            raise _UnreadableError(msg)
        rewritten = pull.head == pull.base
        content_head = pull.earlier("HeadRefForcePushedEvent", pull.head) if rewritten else pull.head
        if not content_head:
            msg = "the PR head carries no diff and no earlier head is recorded in its force-push history"
            raise _UnreadableError(msg)
        merge_base = self._merge_base(slug, pull, content_head)
        snapshot = PrContentSnapshot(
            merged=True,
            merge_commit=pull.merge_commit,
            content_head=content_head,
            merge_base=merge_base,
            default_branch=pull.default_branch,
            default_tip=pull.default_tip,
            content_head_on_default=self._in_history(slug, pull.default_tip, content_head),
            merge_commit_on_default=not rewritten and self._in_history(slug, pull.default_tip, pull.merge_commit),
            base_blobs=self._tree(slug, merge_base),
            head_blobs=self._tree(slug, content_head),
            merge_blobs=self._tree(slug, pull.merge_commit),
            tip_blobs=self._tree(slug, pull.default_tip),
        )
        if self._branch_tip(slug, pull.default_branch) != pull.default_tip:
            msg = f"the default branch {pull.default_branch} moved during the content probe"
            raise _UnreadableError(msg)
        return snapshot

    def _read(self, argv: list[str], *, what: str) -> RawAPIDict:
        return _parsed(*self._run(argv), what=what)

    def _pull(self, slug: str, pr_id: int) -> _Pull:
        owner, _, name = slug.partition("/")
        argv = ["api", "graphql", "-f", f"query={_QUERY}", "-F", f"number={pr_id}", "-f", f"owner={owner}"]
        payload = self._read([*argv, "-f", f"name={name}"], what="the pull request query")
        if errors := payload.get("errors"):
            messages = [_text(error, "message") for error in errors] if isinstance(errors, list) else [str(errors)]
            msg = f"the pull request query returned GraphQL errors: {'; '.join(messages)}"
            raise _UnreadableError(msg)
        repo, pull = _at(payload, "data", "repository"), _at(payload, "data", "repository", "pullRequest")
        if not isinstance(pull, dict) or not _text(repo, "defaultBranchRef", "target", "oid"):
            msg = "the pull request query named no pull request or default branch"
            raise _UnreadableError(msg)
        nodes = _at(pull, "timelineItems", "nodes")
        return _Pull(
            merged=pull.get("merged") is True,
            merge_commit=_text(pull, "mergeCommit", "oid"),
            head=_text(pull, "headRefOid"),
            base=_text(pull, "baseRefOid"),
            default_branch=_text(repo, "defaultBranchRef", "name"),
            default_tip=_text(repo, "defaultBranchRef", "target", "oid"),
            force_pushes=tuple(
                _ForcePush(
                    _text(node, "__typename"),
                    _text(node, "createdAt"),
                    _text(node, "beforeCommit", "oid"),
                    _text(node, "afterCommit", "oid"),
                )
                for node in (nodes if isinstance(nodes, list) else [])
            ),
        )

    def _compare(self, slug: str, base: str, head: str) -> RawAPIDict | None:
        """``None`` when the two commits share no ancestor — a definite answer, unlike any other failure."""
        rc, out, err = self._run(["api", f"repos/{slug}/compare/{base}...{head}"])
        if rc != 0 and _NO_COMMON_ANCESTOR in out + err:
            return None
        return _parsed(rc, out, err, what=f"compare {base[:8]}...{head[:8]}")

    def _merge_base(self, slug: str, pull: _Pull, content_head: str) -> str:
        compared = self._compare(slug, pull.base, content_head)
        if compared is None and (earlier_base := pull.earlier("BaseRefForcePushedEvent", pull.base)):
            compared = self._compare(slug, earlier_base, content_head)
        merge_base = _text(compared, "merge_base_commit", "sha")
        if not merge_base:
            msg = f"no merge base between the PR base and the content head {content_head[:8]}"
            raise _UnreadableError(msg)
        return merge_base

    def _in_history(self, slug: str, tip: str, sha: str) -> bool:
        compared = self._compare(slug, tip, sha)
        status = _text(compared, "status")
        if compared is None or status in _DIVERGED_STATUSES:
            return False
        if status in _ANCESTOR_STATUSES:
            return True
        msg = f"compare {tip[:8]}...{sha[:8]} returned an unknown status {status!r}"
        raise _UnreadableError(msg)

    def _tree(self, slug: str, sha: str) -> dict[str, str]:
        payload = self._read(["api", f"repos/{slug}/git/trees/{sha}?recursive=1"], what=f"the tree of {sha[:8]}")
        entries = payload.get("tree")
        if payload.get("truncated") is not False or not isinstance(entries, list):
            msg = f"the tree of {sha[:8]} is truncated or missing"
            raise _UnreadableError(msg)
        return {
            _text(entry, "path"): _text(entry, "sha")
            for entry in entries
            if _text(entry, "type") in {"blob", "commit"} and _text(entry, "path") and _text(entry, "sha")
        }

    def _branch_tip(self, slug: str, branch: str) -> str:
        return _text(
            self._read(["api", f"repos/{slug}/git/ref/heads/{branch}"], what=f"the {branch} ref"), "object", "sha"
        )
