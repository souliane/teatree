"""Auto-dispatch a Claude cold-review on every self-authored PR push (#3569).

The Claude counterpart to :class:`~teatree.loop.scanners.codex_review.CodexReviewScanner`.
On a host with no ``codex`` binary the fleet-of-agents self-review doublecheck
must still run: the loop dispatches the ``t3:reviewer`` sub-agent to cold-review
the user's own open PRs. The factory picks this scanner over the codex one via
``scanner_factories._self_pr_review_scanner_for`` when the resolved
``self_pr_review_backend`` is ``claude`` (the ``auto`` default on a codex-less
box).

The scanner emits one ``self_pr_review.dispatch`` :class:`ScanSignal` per open
non-draft self-authored PR, UNCONDITIONALLY every tick — the per-SHA idempotency
is enforced DOWNSTREAM at persist time, where ``persistence._handle_reviewer``'s
self-PR branch claims one :class:`CodexReviewMarker` in the same transaction that
creates the reviewing Task. A dropped/failed persist rolls the marker back and
the next tick retries; a force-push (new head SHA) re-fires the review. This is
the exact idempotency shape the codex path uses — never the pre-#1254 per-tick
flood the marker key (``slug``, ``pr_id``, ``head_sha``) forecloses.
"""

import logging
from dataclasses import dataclass

from teatree.loop.scanners.base import ScannerError, ScanSignal
from teatree.loop.scanners.codex_review import CodexPrApi, PrSummary, is_adversarial_review

logger = logging.getLogger(__name__)

#: The default Claude self-PR review — for ordinary diffs.
CLAUDE_STANDARD_REVIEW_VARIANT = "claude:review"

#: The hardened Claude self-PR review — for high-stakes diffs (auth, migrations,
#: secrets) or an untrusted public author.
CLAUDE_ADVERSARIAL_REVIEW_VARIANT = "claude:adversarial-review"


@dataclass(slots=True)
class ClaudeSelfPrReviewScanner:
    """Emit ``self_pr_review.dispatch`` signals routing self-PRs to ``t3:reviewer``.

    *repos* is the ordered list of GitHub ``owner/repo`` slugs swept every tick.
    *api* lists open self-authored PRs through ``gh`` (only the user's own PRs
    need the doublecheck; colleague PRs go through the ordinary reviewer
    pipeline). *overlay* tags emitted signals so a multi-overlay loop attributes
    the dispatch to the right overlay.
    """

    repos: tuple[str, ...]
    api: CodexPrApi
    overlay: str = ""
    name: str = "self_pr_review"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for slug in self.repos:
            for pr in self._safe_list(slug):
                try:
                    signal = self._evaluate(pr)
                except Exception:
                    # Isolate each PR: one PR whose classification raises must not
                    # drop the review dispatch for the other PRs in the sweep.
                    logger.exception("self_pr_review failed to evaluate %s#%d", pr.slug, pr.number)
                    continue
                if signal is not None:
                    signals.append(signal)
                    logger.info(
                        "self_pr_review dispatch %s#%d head=%s variant=%s",
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
            logger.exception("self_pr_review failed to list PRs for %s", slug)
            return []

    def _evaluate(self, pr: PrSummary) -> ScanSignal | None:
        if pr.is_draft:
            return None
        adversarial = is_adversarial_review(pr.changed_files, slug=pr.slug, author=pr.author)
        variant = CLAUDE_ADVERSARIAL_REVIEW_VARIANT if adversarial else CLAUDE_STANDARD_REVIEW_VARIANT
        return ScanSignal(
            kind="self_pr_review.dispatch",
            summary=f"self-PR review {pr.slug}#{pr.number} @ {pr.head_sha[:8]} ({variant})",
            payload={
                "slug": pr.slug,
                "pr_id": pr.number,
                "head_sha": pr.head_sha,
                # Both keys: the reviewer handler reads ``url``, the self-PR branch
                # reads ``pr_url`` — mirroring the codex payload contract.
                "pr_url": pr.url,
                "url": pr.url,
                "variant": variant,
                "overlay": self.overlay,
                "title": pr.title,
                "self_pr": True,
            },
        )


__all__ = [
    "CLAUDE_ADVERSARIAL_REVIEW_VARIANT",
    "CLAUDE_STANDARD_REVIEW_VARIANT",
    "ClaudeSelfPrReviewScanner",
]
