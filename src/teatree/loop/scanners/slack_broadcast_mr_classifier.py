"""Production MR-state classifier for the Slack broadcast scanner (#1131).

The broadcast scanner
(:class:`teatree.loop.scanners.slack_broadcasts.SlackBroadcastsScanner`) is
injected with an ``MrStateClassifier`` — a function mapping MR/PR URLs to
per-URL :class:`~teatree.loop.scanners.slack_broadcasts.MrState` records. This
module holds the production implementation: :class:`ForgeMrStateClassifier`
reads GitLab through HTTP and GitHub through the backend CLI transport, then
turns each payload into a verdict.
"""

import json
from collections.abc import Sequence
from typing import cast
from urllib.parse import quote

import httpx

from teatree.forge_credentials import ForgeTokenState, resolve_url_token
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, classify_gh_stderr, classify_http_error, run_gh
from teatree.loop.scanners.gitlab_http import gitlab_api_for
from teatree.loop.scanners.slack_broadcasts import MrState
from teatree.types import RawAPIDict
from teatree.url_classify import Forge, forge_of, repo_and_iid

__all__ = ["ForgeMrStateClassifier"]


def _classifier_error(error_class: ScannerErrorClass, detail: str) -> ScannerError:
    """Build the :class:`ScannerError` the classifier raises on a non-verdict failure (F5.3)."""
    return ScannerError(scanner="slack_broadcasts", error_class=error_class, detail=detail)


def _parse_classifier_json(stdout: str, *, tool: str, url: str) -> RawAPIDict:
    """Parse a classifier subprocess's JSON object, raising :class:`ScannerError` on garbage (F5.3).

    Empty output, non-JSON text, and a well-formed-but-non-object payload are
    all failures-to-reach-a-verdict, not "not merged": the caller must not read
    ``merged=False`` out of them. Only a parsed JSON *object* is a verdict.
    """
    if not stdout.strip():
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned empty output")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned non-JSON output: {exc}") from exc
    if not isinstance(data, dict):
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned non-object JSON")
    return cast("RawAPIDict", data)


def _classifier_str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _classifier_int(data: RawAPIDict, key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) else 0


def _classifier_head_sha(data: RawAPIDict, *, key: str) -> str:
    """The MR's head commit from the payload the classifier already fetched.

    GitLab MR JSON carries the head at top-level ``sha``
    with ``diff_refs.head_sha`` as the authoritative sibling; GitHub's
    ``gh pr view --json headRefOid`` carries ``headRefOid``. An unreadable
    value degrades to ``""`` — a fail-open "unknown head" the re-review path
    never mistakes for a moved head.
    """
    direct = data.get(key)
    if isinstance(direct, str) and direct:
        return direct
    diff_refs = data.get("diff_refs")
    if isinstance(diff_refs, dict):
        nested = cast("RawAPIDict", diff_refs).get("head_sha")
        if isinstance(nested, str):
            return nested
    return ""


def _classifier_author(data: RawAPIDict, *, key: str) -> str:
    """The forge author's username from ``data["author"][key]`` (GitLab ``username`` / GitHub ``login``)."""
    author = data.get("author")
    if not isinstance(author, dict):
        return ""
    value = cast("RawAPIDict", author).get(key)
    return value if isinstance(value, str) else ""


class ForgeMrStateClassifier:
    """Production :class:`MrStateClassifier` for GitLab MRs and GitHub PRs.

    GitLab metadata is read over the token-authenticated HTTP backend;
    GitHub metadata uses the backend's gh runner. The classifier reads
    ``state`` (merged-or-not) and a coarse
    approval flag (GitLab ``upvotes > 0``, GitHub
    ``reviewDecision == APPROVED``).

    Transient vs verdict (F5.3): ``merged=False`` is a *verdict* — the read
    ran, returned a parseable payload, and the MR was not merged — so the
    scanner may safely dispatch a reviewer / seed a nag row for it. A
    FAILURE to reach that verdict (a missing GitHub binary, an expired
    token, an HTTP or CLI error, or invalid JSON) is NOT a verdict:
    the classifier raises :class:`ScannerError` so the dispatcher records
    the degradation (#1287) and skips the tick, instead of silently
    classifying a possibly-MERGED MR as open — which would nag reviewers
    about already-landed work. A URL that is not a recognised MR (unparsable
    forge / IID) stays ``merged=False`` (it is a deterministic non-match, not
    a transient failure).

    Every token is resolved from the overlay owning the URL. The owning
    overlay also supplies the GitLab API host.
    """

    def __call__(self, urls: Sequence[str]) -> list[MrState]:
        return [self._classify_one(url) for url in urls]

    def _classify_one(self, url: str) -> MrState:
        forge = forge_of(url)
        if forge is Forge.GITLAB:
            return self._classify_gitlab(url)
        if forge is Forge.GITHUB:
            return self._classify_github(url)
        return MrState(url=url, merged=False, approved=False)

    @staticmethod
    def _classify_gitlab(url: str) -> MrState:
        parsed = repo_and_iid(url)
        if parsed is None:
            return MrState(url=url, merged=False, approved=False)
        project, iid_num = parsed
        iid = str(iid_num)
        resolution = resolve_url_token(url, credential="gitlab_token")
        if resolution.state is not ForgeTokenState.TOKEN or not resolution.token:
            raise _classifier_error(
                ScannerErrorClass.AUTH,
                f"{resolution.setting} for {url!r} is {resolution.state.value}: {resolution.detail}",
            )
        try:
            payload = gitlab_api_for(resolution).get_json(f"projects/{quote(project, safe='')}/merge_requests/{iid}")
        except (httpx.HTTPError, ValueError) as exc:
            raise _classifier_error(classify_http_error(exc), f"GitLab MR {url!r}: {exc}") from exc
        if not isinstance(payload, dict):
            raise _classifier_error(ScannerErrorClass.UNKNOWN, f"GitLab MR {url!r} returned non-object JSON")
        data = payload
        state = _classifier_str(data, "state").lower()
        merged = state in {"merged", "closed_as_merged"}
        upvotes = _classifier_int(data, "upvotes")
        approved = upvotes > 0 or merged
        author_username = _classifier_author(data, key="username")
        return MrState(
            url=url,
            merged=merged,
            approved=approved,
            author_username=author_username,
            head_sha=_classifier_head_sha(data, key="sha"),
        )

    @staticmethod
    def _classify_github(url: str) -> MrState:
        resolution = resolve_url_token(url, credential="github_token")
        if resolution.state is not ForgeTokenState.TOKEN or not resolution.token:
            raise _classifier_error(
                ScannerErrorClass.AUTH,
                f"{resolution.setting} for {url!r} is {resolution.state.value}: {resolution.detail}; "
                "refusing ambient gh authentication",
            )
        result = run_gh(["pr", "view", url, "--json", "state,reviewDecision,author,headRefOid"], resolution=resolution)
        if result.returncode != 0:
            raise _classifier_error(
                classify_gh_stderr(result.stderr),
                f"gh pr view {url!r} rc={result.returncode}: {result.stderr.strip()[:200]}",
            )
        data = _parse_classifier_json(result.stdout, tool="gh pr view", url=url)
        state = _classifier_str(data, "state").upper()
        review_decision = _classifier_str(data, "reviewDecision").upper()
        merged = state == "MERGED"
        approved = review_decision == "APPROVED" or merged
        author_username = _classifier_author(data, key="login")
        return MrState(
            url=url,
            merged=merged,
            approved=approved,
            author_username=author_username,
            head_sha=_classifier_head_sha(data, key="headRefOid"),
        )
