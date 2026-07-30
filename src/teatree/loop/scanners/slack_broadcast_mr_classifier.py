"""Production MR-state classifier for the Slack broadcast scanner (#1131).

The broadcast scanner
(:class:`teatree.loop.scanners.slack_broadcasts.SlackBroadcastsScanner`) is
injected with an ``MrStateClassifier`` — a function mapping MR/PR URLs to
per-URL :class:`~teatree.loop.scanners.slack_broadcasts.MrState` records. This
module holds the production implementation: :class:`GlabGhMrStateClassifier`
shells out to ``glab mr view`` / ``gh pr view`` per host, plus the JSON-parsing
helpers that turn a subprocess payload into a verdict. Keeping the shell-out
classifier in its own module keeps the scanner unit-testable without ``glab`` /
``gh`` and focused on the broadcast decision path.
"""

import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, classify_gh_stderr
from teatree.loop.scanners.slack_broadcasts import MrState
from teatree.types import RawAPIDict
from teatree.url_classify import Forge, forge_of, repo_and_iid

__all__ = ["GlabGhMrStateClassifier"]


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

    GitLab's ``glab mr view -F json`` carries the head at top-level ``sha``
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


@dataclass(slots=True)
class GlabGhMrStateClassifier:
    """Production :class:`MrStateClassifier` — shells out to ``glab`` / ``gh``.

    Each URL is dispatched by host: ``glab mr view <url> -F json`` for
    GitLab merge requests, ``gh pr view <url> --json …`` for GitHub
    pulls. The classifier reads ``state`` (merged-or-not) and a coarse
    approval flag (GitLab ``upvotes > 0``, GitHub
    ``reviewDecision == APPROVED``).

    Transient vs verdict (F5.3): ``merged=False`` is a *verdict* — the tool
    ran, returned a parseable payload, and the MR was not merged — so the
    scanner may safely dispatch a reviewer / seed a nag row for it. A
    FAILURE to reach that verdict (the binary is missing, the token is
    expired / rc≠0, or the output is not parseable JSON) is NOT a verdict:
    the classifier raises :class:`ScannerError` so the dispatcher records
    the degradation (#1287) and skips the tick, instead of silently
    classifying a possibly-MERGED MR as open — which would nag reviewers
    about already-landed work. A URL that is not a recognised MR (unparsable
    forge / IID) stays ``merged=False`` (it is a deterministic non-match, not
    a transient failure).

    Tokens are optional: when set they're exported as ``GITLAB_TOKEN`` /
    ``GH_TOKEN`` for each subprocess so a private-repo overlay can
    classify on behalf of its own PAT.
    """

    glab_token: str = ""
    github_token: str = ""

    def __call__(self, urls: Sequence[str]) -> list[MrState]:
        return [self._classify_one(url) for url in urls]

    def _classify_one(self, url: str) -> MrState:
        forge = forge_of(url)
        if forge is Forge.GITLAB:
            return self._classify_gitlab(url)
        if forge is Forge.GITHUB:
            return self._classify_github(url)
        return MrState(url=url, merged=False, approved=False)

    def _classify_gitlab(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: loaded at tick time, not import

        parsed = repo_and_iid(url)
        if parsed is None:
            return MrState(url=url, merged=False, approved=False)
        project, iid_num = parsed
        iid = str(iid_num)
        glab = shutil.which("glab") or "glab"
        env = {**os.environ, "GITLAB_TOKEN": self.glab_token} if self.glab_token else None
        try:
            # ``-R <project>`` makes glab resolve the MR against an explicit
            # project path instead of the current cwd's git remote — the
            # scanner runs from the loop process which has no repo cwd, so
            # ``glab mr view <url>`` (URL-only) silently exits non-zero and
            # every broadcast is dropped. With ``-R`` + numeric IID glab
            # routes the API call directly.
            result = run_allowed_to_fail(
                [glab, "mr", "view", "-R", project, iid, "-F", "json"],
                expected_codes=None,
                env=env,
            )
        except FileNotFoundError as exc:
            raise _classifier_error(ScannerErrorClass.UNKNOWN, f"glab not installed for {url!r}: {exc}") from exc
        if result.returncode != 0:
            raise _classifier_error(
                classify_gh_stderr(result.stderr),
                f"glab mr view {url!r} rc={result.returncode}: {result.stderr.strip()[:200]}",
            )
        data = _parse_classifier_json(result.stdout, tool="glab mr view", url=url)
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

    def _classify_github(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: loaded at tick time, not import

        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.github_token} if self.github_token else None
        try:
            result = run_allowed_to_fail(
                [gh, "pr", "view", url, "--json", "state,reviewDecision,author,headRefOid"],
                expected_codes=None,
                env=env,
            )
        except FileNotFoundError as exc:
            raise _classifier_error(ScannerErrorClass.UNKNOWN, f"gh not installed for {url!r}: {exc}") from exc
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
