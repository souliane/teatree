"""``t3 review run <MR_URL>`` — review-shape audit (#1206).

A read-only CLI that fetches an MR's diff metadata, existing-review
state, and a small finding catalog, then prints a structured JSON
summary to stdout. The output is the durable handoff the skill prompts
can rely on instead of every reviewer sub-agent improvising its own
diff-fetch + checklist.

The command itself never publishes anything — it routes through
:class:`~teatree.backends.gitlab.api.GitLabAPI` GET endpoints only — so
it stays outside the on-behalf approval surface (#960). The reviewer
sub-agent consumes the JSON and decides what to post via
``t3 review post-draft-note`` / ``post-comment`` afterwards.

Output schema (one JSON object on stdout):

``{
    "mr": "<repo>!<iid>",
    "forge": "gitlab",
    "url": "<input url>",
    "changes": {"files": int, "additions": int, "deletions": int},
    "complexity": "trivial"|"small"|"moderate"|"large",
    "existing_review": {"open_discussions": int, "draft_notes": int,
                                            "approvals": int, "approved_by": [str, ...]},
    "findings_catalog": [str, ...],
    "verdict": "ready_to_review"|"needs_attention",
}``

GitHub PRs return exit code 2 with ``{"error": "unsupported_forge",
"forge": "github"}`` — explicit "not yet implemented", never a
masquerading success. Forge resolution is structural (URL shape), not
heuristic. Other malformed URLs exit 2 with ``error="bad_url"``.
"""

import json
from dataclasses import dataclass
from typing import Final, cast

import typer

from teatree.cli.review.service import review_app
from teatree.url_classify import Forge, forge_of, repo_and_iid
from teatree.utils.django_bootstrap import ensure_django

# GitLab JSON payloads — narrow ``object`` rather than a fictitious schema
# because the API surface mixes strings (paths, diffs), ints (ids), and
# nested dicts/lists per endpoint. Mirrors the type-alias pattern in
# :mod:`teatree.cli.review.diff`.
type JSONObject = dict[str, object]
type DiscussionList = list[JSONObject]

_LARGE_LOC: Final = 500
_MODERATE_LOC: Final = 200
_SMALL_LOC: Final = 50
_LARGE_FILES: Final = 20

_TEST_PATH_MARKERS: Final = ("test", "tests/", "_test.", "spec/")


@dataclass(frozen=True, slots=True)
class _DiffStats:
    """Per-MR aggregated diff counts and touched-path list."""

    files: int
    additions: int
    deletions: int
    touched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReviewState:
    """Existing-review surface counts."""

    open_discussions: int
    draft_notes: int
    approvals: int
    approved_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewRunResult:
    """The audit result printed as JSON to stdout."""

    mr: str
    forge: str
    url: str
    diff: _DiffStats
    complexity: str
    state: _ReviewState
    findings: tuple[str, ...]
    verdict: str

    def to_json(self) -> str:
        payload = {
            "mr": self.mr,
            "forge": self.forge,
            "url": self.url,
            "changes": {
                "files": self.diff.files,
                "additions": self.diff.additions,
                "deletions": self.diff.deletions,
            },
            "complexity": self.complexity,
            "existing_review": {
                "open_discussions": self.state.open_discussions,
                "draft_notes": self.state.draft_notes,
                "approvals": self.state.approvals,
                "approved_by": list(self.state.approved_by),
            },
            "findings_catalog": list(self.findings),
            "verdict": self.verdict,
        }
        return json.dumps(payload, sort_keys=True)


def _classify_complexity(*, files: int, additions: int, deletions: int) -> str:
    total = additions + deletions
    if files >= _LARGE_FILES or total >= _LARGE_LOC:
        return "large"
    if total >= _MODERATE_LOC:
        return "moderate"
    if total >= _SMALL_LOC:
        return "small"
    return "trivial"


def _gather_findings(*, complexity: str, files: int, touched_paths: tuple[str, ...]) -> tuple[str, ...]:
    findings: list[str] = []
    if complexity == "large":
        findings.append(f"large change ({files} files) — consider splitting into focused MRs")
    test_touched = any(any(marker in path.lower() for marker in _TEST_PATH_MARKERS) for path in touched_paths)
    if touched_paths and not test_touched:
        findings.append("no test files touched — confirm coverage is intentional")
    return tuple(findings)


def _open_discussion_count(discussions: DiscussionList) -> int:
    """Count discussion threads with at least one unresolved note."""
    count = 0
    for discussion in discussions:
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            note_dict = cast("JSONObject", note)
            if "resolved" in note_dict and note_dict.get("resolved") is False:
                count += 1
                break
    return count


def _count_diff_lines(diff_text: str) -> tuple[int, int]:
    """Return ``(additions, deletions)`` for a unified-diff snippet."""
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _diff_stats_from_changes(changes_payload: object) -> _DiffStats:
    """Aggregate GitLab ``/changes`` response into a :class:`_DiffStats`."""
    if not isinstance(changes_payload, dict):
        return _DiffStats(files=0, additions=0, deletions=0, touched=())
    payload = cast("JSONObject", changes_payload)
    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        return _DiffStats(files=0, additions=0, deletions=0, touched=())
    additions = 0
    deletions = 0
    touched: list[str] = []
    for change in raw_changes:
        if not isinstance(change, dict):
            continue
        change_dict = cast("JSONObject", change)
        diff_text = change_dict.get("diff")
        if isinstance(diff_text, str):
            added, removed = _count_diff_lines(diff_text)
            additions += added
            deletions += removed
        new_path = change_dict.get("new_path")
        if isinstance(new_path, str) and new_path:
            touched.append(new_path)
    return _DiffStats(files=len(raw_changes), additions=additions, deletions=deletions, touched=tuple(touched))


class _ReviewRunAPIError(RuntimeError):
    """The GitLab API refused or returned an unusable response — audit cannot run.

    Distinct from :class:`ValueError` (bad URL): the URL parsed, but
    the API surface refused our GETs (no token, repo not found, etc.).
    The CLI surfaces this as a structured ``{"error": "api_unavailable"}``
    exit-1 payload so the reviewer sub-agent never reads "ready_to_review"
    on data that was never fetched.
    """


def _fetch_review_state(api: object, repo: str, iid: int) -> _ReviewState:
    """Aggregate existing-review counts for the MR."""
    resolve_project = getattr(api, "resolve_project", None)
    project = resolve_project(repo) if callable(resolve_project) else None
    if project is None:
        msg = f"resolve_project({repo!r}) returned None — token missing or repo inaccessible"
        raise _ReviewRunAPIError(msg)
    project_id = project.project_id
    discussions = api.get_mr_discussions(project_id, iid)  # type: ignore[attr-defined]
    draft_count = api.get_draft_notes_count(project_id, iid)  # type: ignore[attr-defined]
    approvals = api.get_mr_approvals(project_id, iid)  # type: ignore[attr-defined]
    approvals_count = int(approvals.get("count", 0))
    names = approvals.get("approved_by")
    approver_names: tuple[str, ...] = tuple(str(n) for n in names) if isinstance(names, list) else ()
    return _ReviewState(
        open_discussions=_open_discussion_count(discussions),
        draft_notes=int(draft_count) if draft_count is not None else 0,
        approvals=approvals_count,
        approved_by=approver_names,
    )


def _audit_gitlab_mr(url: str) -> ReviewRunResult:
    """Fetch metadata for a GitLab MR and build the audit result.

    Every GitLab GET is wrapped: backend exceptions (``httpx.HTTPStatusError``
    for 401/403/404, ``httpx.RequestError`` for connection failures) are
    normalized into :class:`_ReviewRunAPIError` so the CLI surfaces a
    structured ``api_unavailable`` payload rather than a raw traceback.
    """
    import httpx  # noqa: PLC0415

    from teatree.backends.gitlab.api import GitLabAPI  # noqa: PLC0415
    from teatree.cli.review.service import ReviewService  # noqa: PLC0415
    from teatree.core.models.live_post_approval import canonical_mr_scope  # noqa: PLC0415

    parsed = repo_and_iid(url)
    if parsed is None:
        msg = "bad_url"
        raise ValueError(msg)
    repo, iid = parsed
    encoded = repo.replace("/", "%2F")
    api = GitLabAPI(token=ReviewService.get_gitlab_token(), base_url=ReviewService._resolve_base_url())  # noqa: SLF001

    try:
        changes_payload = api.get_json(f"projects/{encoded}/merge_requests/{iid}/changes")
        if changes_payload is None:
            msg = f"GET /changes returned no payload for {repo}!{iid} — token missing or MR inaccessible"
            raise _ReviewRunAPIError(msg)
        diff = _diff_stats_from_changes(changes_payload)
        state = _fetch_review_state(api, repo, iid)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        msg = f"GitLab backend refused the audit for {repo}!{iid}: {exc}"
        raise _ReviewRunAPIError(msg) from exc
    complexity = _classify_complexity(files=diff.files, additions=diff.additions, deletions=diff.deletions)
    findings = _gather_findings(complexity=complexity, files=diff.files, touched_paths=diff.touched)
    verdict = "needs_attention" if findings or state.open_discussions else "ready_to_review"
    return ReviewRunResult(
        mr=canonical_mr_scope(url),
        forge="gitlab",
        url=url,
        diff=diff,
        complexity=complexity,
        state=state,
        findings=findings,
        verdict=verdict,
    )


@review_app.command(name="run")
def run(
    url: str = typer.Argument(help="GitLab MR URL (GitHub PR URLs return unsupported_forge)."),
) -> None:
    """Run the review-shape audit for an MR and print a JSON summary.

    Read-only: this command never posts to GitLab or GitHub. It fetches
    diff metadata, existing-review state (discussions + draft notes +
    approvals), classifies complexity, and emits a small findings
    catalog. The reviewer sub-agent consumes the JSON and decides what
    to do next via ``t3 review post-draft-note`` / ``post-comment``.

    Exit codes:

    * ``0`` — audit ran, JSON printed.
    * ``1`` — URL parsed but the GitLab API refused the audit
        (``api_unavailable``: missing token, 401/403/404, connection
        failure, or any other backend error).
    * ``2`` — URL refused before any API call (``unsupported_forge`` for
        GitHub PRs, ``bad_url`` for anything else).
    """
    forge = forge_of(url)
    if forge is Forge.GITHUB:
        typer.echo(json.dumps({"error": "unsupported_forge", "forge": "github", "url": url}, sort_keys=True))
        raise typer.Exit(code=2)
    if forge is not Forge.GITLAB:
        typer.echo(json.dumps({"error": "bad_url", "url": url}, sort_keys=True))
        raise typer.Exit(code=2)
    ensure_django()
    try:
        result = _audit_gitlab_mr(url)
    except ValueError:
        typer.echo(json.dumps({"error": "bad_url", "url": url}, sort_keys=True))
        raise typer.Exit(code=2) from None
    except _ReviewRunAPIError as exc:
        typer.echo(json.dumps({"error": "api_unavailable", "url": url, "detail": str(exc)}, sort_keys=True))
        raise typer.Exit(code=1) from None
    typer.echo(result.to_json())
