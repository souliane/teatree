"""GitLab arm of the PR sweep's :class:`PrApiClient` port, plus the forge router (#72).

:class:`~teatree.loop.scanners.pr_sweep_adapters.GhPrApiClient` shells out to ``gh``.
A slug carries no host, so on a GitLab project that call authenticated against the
wrong forge, raised, and was caught and logged — the sweep reported success having
enumerated nothing, for every MR this fork has ever opened.

:class:`GlabPrApiClient` is the GitLab twin, reading through the same
``CodeHostBackend`` the §17.4 keystone merges on rather than a second hand-rolled
transport. :class:`ForgePrApiClient` is what the scanner is given: it routes each
slug to the arm that slug's forge speaks, resolving the forge from host-BEARING
declarations only (:func:`~teatree.core.merge.host_kind.forge_for_repo_slug`) and
raising rather than guessing when none answers. It is the sibling of #93's
``ForgeMainCiStatus``, one call site over.

Every unanswerable read here is a :class:`ScannerError`, never an empty list: the
dispatcher records it on the tick report and DMs the owner once per day per
``(scanner, error_class)``. An empty list means "this repo has no open MRs" and
nothing else — the conflation of those two is the whole defect.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx

from teatree.core.backend_registry import get_backend_provider
from teatree.core.merge import MergePreconditionError, execute_bound_merge
from teatree.core.merge.host_kind import forge_for_repo_slug
from teatree.loop.scanners.pr_sweep_ports import PrApiClient
from teatree.loop.scanners.pr_sweep_types import PrSummary
from teatree.types import RawAPIDict, ScannerError, ScannerErrorClass
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

logger = logging.getLogger(__name__)

_SCANNER = "pr_sweep"

#: GitLab settles mergeability asynchronously; until it does, ``has_conflicts`` is
#: reported ``false`` for a genuinely conflicted MR. Only a SETTLED status is read,
#: mirroring the GitHub arm's refusal to flag a still-computing ``UNKNOWN``.
_UNSETTLED_MERGE_STATUSES = frozenset({"", "checking", "unchecked", "preparing"})
_CONFLICTED_MERGE_STATUSES = frozenset({"cannot_be_merged", "broken_status", "conflict"})


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _classify_http(exc: Exception) -> ScannerErrorClass:
    """Map a GitLab transport failure onto the class the dispatcher reports."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            return ScannerErrorClass.AUTH
        if status == httpx.codes.TOO_MANY_REQUESTS:
            return ScannerErrorClass.RATE_LIMIT
        return ScannerErrorClass.UNKNOWN
    if isinstance(exc, httpx.HTTPError):
        return ScannerErrorClass.NETWORK
    return ScannerErrorClass.UNKNOWN


@dataclass(slots=True)
class GlabPrApiClient:
    """GitLab :class:`PrApiClient`, over the registry-resolved ``CodeHostBackend``.

    *token* is the overlay's own PAT, mirroring :class:`GhPrApiClient`'s ``GH_TOKEN``
    export so a private project is read under the identity that overlay declares.
    """

    token: str = ""

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        """Every open MR on *slug*, or a :class:`ScannerError` — never a silent empty.

        The project is resolved FIRST because ``list_prs`` degrades an unresolvable
        project to ``[]``, which the sweep would read as "no open MRs" and skip the
        repo forever. A 404 there is a project this token cannot see; 401/403 and
        transport faults surface from the resolve itself.
        """
        backend = self._backend()
        repo = self._read(lambda: backend.get_repo(repo=slug), slug=slug, what="project lookup")
        if repo.get("error"):
            detail = (
                f"glab project {slug!r} did not resolve: {repo['error']}. The sweep cannot tell "
                f"'no open MRs' from 'no access', so it refuses instead of reporting an empty pass."
            )
            raise ScannerError(scanner=_SCANNER, error_class=ScannerErrorClass.AUTH, detail=detail)
        raw = self._read(lambda: backend.list_prs(repo=slug, state="open"), slug=slug, what="MR list")
        return [_decode_mr(slug=slug, raw=entry) for entry in raw if isinstance(entry, dict)]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:  # noqa: PLR6301 — PrApiClient port; GitLab has no named-check rollup on the default branch.
        """Always ``False`` — the uv-audit fallback is a GitHub-Actions escape hatch.

        It asks whether ONE named check is red on ``main`` so a pre-existing audit
        failure does not block every PR. GitLab aggregates jobs into one pipeline
        status with no per-check verdict to compare against, so the fallback simply
        never fires here — the same "not confirmed failed" direction the GitHub arm
        degrades to on an unreadable read.
        """
        del slug, check_name
        return False

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:  # noqa: PLR6301 — PrApiClient port; the bound merge is a stateless keystone delegate.
        """SHA-bound squash merge on the GitLab transport (#1985's §17.4.3 bind)."""
        try:
            merged_sha = execute_bound_merge(
                ref=PrRef(slug=slug, pr_id=pr_id, host_kind="gitlab"),
                expected_head_oid=expected_head_oid,
            )
        except MergePreconditionError:
            return False, ""
        return True, merged_sha

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:  # noqa: PLR6301 — PrApiClient port; GitLab exposes no SHA-bound update-branch.
        """Always ``False`` — GitLab's rebase endpoint takes no expected-head bind.

        Without that bind a force-push in the TOCTOU window would be rebased blind,
        which is exactly what #4063's ``expected_head_sha`` exists to refuse. The
        caller degrades to the ``needs_branch_update`` flag, so the stale base is
        still surfaced — a human updates the branch.
        """
        logger.info("pr_sweep: no SHA-bound branch update on GitLab for %s!%d @ %s", slug, pr_id, expected_head_oid[:8])
        return False

    def _backend(self) -> "CodeHostBackend":
        return get_backend_provider().build_gitlab_host(token=self.token, base_url="")

    @staticmethod
    def _read[T](call: Callable[[], T], *, slug: str, what: str) -> T:
        try:
            return call()
        except (httpx.HTTPError, ValueError) as exc:
            detail = f"glab {what} for {slug!r} failed: {type(exc).__name__}: {str(exc)[:200]}"
            raise ScannerError(scanner=_SCANNER, error_class=_classify_http(exc), detail=detail) from exc


def _decode_mr(*, slug: str, raw: RawAPIDict) -> PrSummary:
    """One GitLab MR list entry as the sweep's forge-neutral :class:`PrSummary`.

    ``rollup`` is deliberately empty: GitLab has no per-check rollup, so the sweep's
    CI gate reads the head pipeline through ``CodeHostQuery.required_checks_status``
    instead (see ``pr_sweep.PrSweepScanner._ci_gate``). ``behind_main`` stays ``False`` because the
    list payload reports no divergence and the remedy it gates — the SHA-bound
    update-branch — has no GitLab equivalent.
    """
    return PrSummary(
        slug=slug,
        number=_as_int(raw.get("iid")),
        head_sha=_as_str(raw.get("sha")),
        is_draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        has_changes_requested=raw.get("blocking_discussions_resolved") is False,
        url=_as_str(raw.get("web_url")),
        title=_as_str(raw.get("title")),
        is_conflicted=_is_conflicted(raw),
        author=_author_username(raw),
        same_repo=_same_repo(raw),
        host_kind="gitlab",
    )


def _author_username(raw: RawAPIDict) -> str:
    """The MR author's GitLab handle — the auto-review arm's own-PR scope (#2210)."""
    author = raw.get("author")
    if not isinstance(author, dict):
        return ""
    return _as_str(cast("RawAPIDict", author).get("username"))


def _is_conflicted(raw: RawAPIDict) -> bool:
    """True iff GitLab has SETTLED that the MR cannot merge because of a conflict."""
    status = _as_str(raw.get("detailed_merge_status") or raw.get("merge_status")).lower()
    if status in _UNSETTLED_MERGE_STATUSES:
        return False
    return raw.get("has_conflicts") is True or status in _CONFLICTED_MERGE_STATUSES


def _same_repo(raw: RawAPIDict) -> bool | None:
    """Tri-state fork provenance from the MR's source/target project ids (#3244)."""
    source, target = raw.get("source_project_id"), raw.get("target_project_id")
    if not isinstance(source, int) or not isinstance(target, int):
        return None
    return source == target


@dataclass(frozen=True, slots=True)
class ForgePrApiClient:
    """Route each slug to the :class:`PrApiClient` arm its own forge speaks (#72).

    The scanner holds ONE client for a repo list that need not share a forge, so the
    forge is resolved per slug rather than once for the pass — the same shape #93's
    ``ForgeMainCiStatus`` uses one call site over. Both arms are injected because a
    default would have to name a concrete adapter, and the two carry different PATs.

    A slug no declaration answers for RAISES: guessing a transport probes an
    unrelated repo on the other forge, and answering ``[]`` is the silent no-op this
    class exists to end.
    """

    github: PrApiClient
    gitlab: PrApiClient

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return self._arm(slug).list_open_prs(slug=slug)

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        return self._arm(slug).main_check_failed(slug=slug, check_name=check_name)

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        return self._arm(slug).merge_pr_squash_bound(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:
        return self._arm(slug).update_pr_branch(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)

    def _arm(self, slug: str) -> PrApiClient:
        forge = _resolve_forge(slug)
        if forge == "github":
            return self.github
        if forge == "gitlab":
            return self.gitlab
        detail = (
            f"no forge is declared for {slug!r}, so the sweep cannot pick a transport for it and "
            f"refuses to guess one. Declare the namespace's forge host in the owning overlay's "
            f"`owned_repos` (e.g. {{'gitlab.com': ['<namespace>']}}) — `t3 doctor check` names every "
            f"swept repo that resolves to nothing."
        )
        raise ScannerError(scanner=_SCANNER, error_class=ScannerErrorClass.UNKNOWN, detail=detail)


def _resolve_forge(slug: str) -> str:
    """*slug*'s declared forge, or ``""`` — an ambiguous declaration answers ``""`` too."""
    try:
        return forge_for_repo_slug(slug)
    except MergePreconditionError as exc:
        logger.warning("pr_sweep: ambiguous forge declaration for %s — %s", slug, exc)
        return ""


__all__ = ["ForgePrApiClient", "GlabPrApiClient"]
