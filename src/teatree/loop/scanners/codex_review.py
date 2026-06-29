"""Auto-dispatch ``/codex:review`` on every PR push (#1254).

The user's binding rule "fleet of agents with codex doublecheck" — every
push of a self-authored PR should get an automatic codex review — was
previously enforced as an agent-vigilance rule and silently failed
multiple times. This scanner is the structural fix: the loop, not the
agent, decides when to dispatch ``/codex:review``.

Decision per open self-authored PR:

1. ``draft: true`` → skip (the user is still iterating; auto-review
    would be noise)
2. ``CodexReviewMarker.claim(slug, pr_id, head_sha)`` returns ``None``
    (already dispatched on a previous tick for the same head SHA) → skip
3. Otherwise → emit one ``codex_review.dispatch`` ``ScanSignal`` carrying
    the dispatch variant (``codex:review`` by default, ``codex:adversarial-review``
    when the diff touches a high-stakes path) so the dispatcher can route
    it to the codex review agent.

The CLI surface mirrors the agent zone naming: ``t3 codex review <pr_url>``
spawns the same agent the scanner emits a signal for, so manual fire-and-
forget invocation is available alongside the loop-driven auto-dispatch.

Why a per-SHA marker rather than a PR-level marker: a force-push must
re-fire the codex review (the diff changed). Keying on head_sha makes
the re-fire automatic; a PR-level marker would silently skip the second
review on a meaningful re-push.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast, runtime_checkable

from teatree.core.author_trust import classify_author
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.scanners.base import ScannerError, ScanSignal, classify_gh_stderr
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


_GH_NOT_INSTALLED_RC = 127

#: Path fragments that classify a diff as high-stakes; touching any of
#: them routes the scanner's dispatch to ``codex:adversarial-review`` so
#: the harder review is the default for security-sensitive code paths.
ADVERSARIAL_PATH_MARKERS: frozenset[str] = frozenset(
    {
        "auth/",
        "permissions/",
        "migrations/",
        "secret",
        "credential",
        "token",
    },
)

#: The default codex review slash command — for ordinary diffs.
STANDARD_REVIEW_VARIANT = "codex:review"

#: The hardened codex review slash command — for high-stakes diffs.
ADVERSARIAL_REVIEW_VARIANT = "codex:adversarial-review"


class GhPrJson(TypedDict, total=False):
    """Shape of one ``gh pr list --json …`` entry the scanner consumes."""

    number: int
    headRefOid: str
    isDraft: bool
    url: str
    title: str
    author: "GhAuthorJson"
    files: list[object]


class GhAuthorJson(TypedDict, total=False):
    """Shape of ``GhPrJson.author`` (``gh`` returns ``{"login": ...}``)."""

    login: str


class GhFileJson(TypedDict, total=False):
    """Shape of one entry in ``GhPrJson.files``."""

    path: str


@dataclass(frozen=True, slots=True)
class PrSummary:
    """Decoded subset of a PR's ``gh`` payload the scanner needs."""

    slug: str
    number: int
    head_sha: str
    is_draft: bool
    changed_files: tuple[str, ...]
    url: str = ""
    title: str = ""
    author: str = ""


@runtime_checkable
class CodexPrApi(Protocol):
    """Adapter over ``gh`` listing self-authored open PRs — mockable in tests."""

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]: ...  # pragma: no branch


@dataclass(slots=True)
class CodexReviewScanner:
    """Emit ``codex_review.dispatch`` signals for newly-pushed PR head SHAs.

    *repos* is the ordered list of GitHub ``owner/repo`` slugs the
    scanner sweeps every tick. *api* lists open self-authored PRs
    through ``gh`` (only the user's own PRs need codex doublecheck;
    colleague PRs go through the existing review pipeline). *overlay*
    tags emitted signals so a multi-overlay loop can attribute the
    dispatch to the right overlay.
    """

    repos: tuple[str, ...]
    api: CodexPrApi
    overlay: str = ""
    name: str = "codex_review"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for slug in self.repos:
            for pr in self._safe_list(slug):
                signal = self._evaluate(pr)
                if signal is not None:
                    signals.append(signal)
                    logger.info(
                        "codex_review dispatch %s#%d head=%s variant=%s",
                        pr.slug,
                        pr.number,
                        pr.head_sha[:8],
                        signal.payload.get("variant"),
                    )
        return signals

    def _safe_list(self, slug: str) -> list[PrSummary]:
        try:
            return self.api.list_open_self_prs(slug=slug)
        except ScannerError:
            raise
        except Exception:
            logger.exception("codex_review failed to list PRs for %s", slug)
            return []

    def _evaluate(self, pr: PrSummary) -> ScanSignal | None:
        if pr.is_draft:
            return None
        variant = _classify_variant(pr.changed_files, slug=pr.slug, author=pr.author)
        marker = CodexReviewMarker.claim(
            slug=pr.slug,
            pr_id=pr.number,
            head_sha=pr.head_sha,
            overlay=self.overlay,
            variant=variant,
        )
        if marker is None:
            return None
        return ScanSignal(
            kind="codex_review.dispatch",
            summary=f"codex review {pr.slug}#{pr.number} @ {pr.head_sha[:8]} ({variant})",
            payload={
                "slug": pr.slug,
                "pr_id": pr.number,
                "head_sha": pr.head_sha,
                "pr_url": pr.url,
                "variant": variant,
                "overlay": self.overlay,
                "title": pr.title,
            },
        )


def _classify_variant(changed_files: tuple[str, ...], *, slug: str = "", author: str = "") -> str:
    """Choose the codex review variant based on author trust and diff footprint.

    Routes to ``codex:adversarial-review`` when EITHER the PR is on a PUBLIC
    repo authored by an untrusted identity (#1773 — the untrusted public author
    never gets the lenient self-PR path) OR the diff touches a high-stakes path
    (auth, permissions, migrations, secrets/tokens/credentials). Defaults to
    ``codex:review``. The classifier is intentionally conservative — false
    positives are fine (adversarial review is strictly more thorough), false
    negatives are the actual failure mode.
    """
    if slug and classify_author(slug, author).untrusted:
        return ADVERSARIAL_REVIEW_VARIANT
    for path in changed_files:
        lowered = path.lower()
        if any(marker in lowered for marker in ADVERSARIAL_PATH_MARKERS):
            return ADVERSARIAL_REVIEW_VARIANT
    return STANDARD_REVIEW_VARIANT


@dataclass(slots=True)
class GhCodexPrApi:
    """``gh``-backed :class:`CodexPrApi` — lists self-authored open PRs.

    *token* — when non-empty — is exported as ``GH_TOKEN`` so the scanner
    can hit a private repo on behalf of a given overlay using that
    overlay's PAT. Uses ``--author @me`` to scope to the authenticated
    user — the codex-doublecheck rule applies to the user's own PRs
    only, not to colleague PRs going through the existing review path.
    """

    token: str = ""

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        argv = [
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--author",
            "@me",
            "--json",
            "number,headRefOid,isDraft,url,title,author,files",
        ]
        rc, out, err = self._run_gh(argv)
        if rc == _GH_NOT_INSTALLED_RC:
            return []
        if rc != 0:
            error_class = _classify_gh_stderr(err)
            detail = f"gh pr list {slug!r} rc={rc}: {err.strip()[:200]}"
            raise ScannerError(
                scanner="codex_review",
                error_class=error_class,
                detail=detail,
            )
        if not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        decoded = (_decode_pr(slug=slug, raw=cast("GhPrJson", item)) for item in data if isinstance(item, dict))
        return [pr for pr in decoded if pr is not None]

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        try:
            result = run_allowed_to_fail([gh, *argv], expected_codes=None, env=env)
        except FileNotFoundError:
            return 127, "", "gh not installed"
        return result.returncode, result.stdout, result.stderr


def _decode_pr(*, slug: str, raw: GhPrJson) -> PrSummary | None:
    number_raw = raw.get("number")
    if not isinstance(number_raw, int):
        logger.warning("codex_review: skipping PR with missing/non-int number in %s payload: %r", slug, raw)
        return None
    number = number_raw
    head_sha = _as_str(raw.get("headRefOid"))
    is_draft = bool(raw.get("isDraft"))
    url = _as_str(raw.get("url"))
    title = _as_str(raw.get("title"))
    author_raw = raw.get("author")
    author = _as_str(author_raw.get("login")) if isinstance(author_raw, dict) else ""
    files_raw = raw.get("files")
    files: list[str] = []
    if isinstance(files_raw, list):
        for entry in files_raw:
            if isinstance(entry, dict):
                path = _as_str(cast("GhFileJson", entry).get("path"))
                if path:
                    files.append(path)
    return PrSummary(
        slug=slug,
        number=number,
        head_sha=head_sha,
        is_draft=is_draft,
        changed_files=tuple(files),
        url=url,
        title=title,
        author=author,
    )


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


_classify_gh_stderr = classify_gh_stderr


__all__ = [
    "ADVERSARIAL_REVIEW_VARIANT",
    "STANDARD_REVIEW_VARIANT",
    "CodexPrApi",
    "CodexReviewScanner",
    "GhCodexPrApi",
    "PrSummary",
]
