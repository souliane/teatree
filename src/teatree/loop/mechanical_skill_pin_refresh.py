"""Skill-pin bump handler — the executor for ``skill_pin.behind`` (#4677).

The scanner (:mod:`teatree.loop.scanners.skill_pin_refresh`) only FLAGS that a
first-party source has moved past its declared pin; this opens the PR that rewrites
``apm.yml``. It deliberately does NOT mutate the running host: ``apm.yml`` stays the
single source of truth, the drift gate keeps comparing installed bytes against a
DECLARED ref, the change is reviewable and revertable like any other, and ``t3 setup``
installs the new pin on its next run — which the container entrypoint performs on
every start.

Idempotent on the HEAD rather than on the tick: the branch name carries the source and
the head sha, so a condition persisting across ticks opens one PR and a source that
moves again opens another. An open PR on that branch, or the branch already existing on
the remote, is a no-op.

Best-effort throughout — a git or forge failure is logged and swallowed, because a down
forge must never abort a loop tick.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.push.fast_push import ForgeClient, GhForge
from teatree.loop.dispatch import ActionPayload
from teatree.utils.git_worktree import worktree_add_at_ref, worktree_remove
from teatree.utils.run import CommandFailedError, run_checked

logger = logging.getLogger(__name__)

_APM_MANIFEST = "apm.yml"
_BRANCH_PREFIX = "skill-pin-refresh"
_SHORT_SHA = 7
_REMOTE = "origin"


@dataclass(frozen=True, slots=True)
class BumpResult:
    """What one ``skill_pin.behind`` signal produced."""

    branch: str = ""
    pr_url: str = ""
    skipped: str = ""


@dataclass(slots=True)
class SkillPinBumper:
    """Open one apm.yml bump PR for a source that has moved past its pin."""

    repo: Path
    forge: ForgeClient
    remote: str = _REMOTE

    def bump(self, payload: ActionPayload) -> BumpResult:
        specs = list(payload.get("specs", []))
        bumped = list(payload.get("bumped_specs", []))
        head = str(payload.get("head", ""))
        if not (specs and bumped and head) or len(specs) != len(bumped):
            return BumpResult(skipped="the signal named no spec to bump")

        branch = f"{_BRANCH_PREFIX}/{str(payload.get('source', '')).replace('/', '-')}-{head[:_SHORT_SHA]}"
        if self.forge.find_pr_url(branch=branch):
            return BumpResult(branch=branch, skipped="a PR for this head is already open")
        if self._remote_branch_exists(branch):
            return BumpResult(branch=branch, skipped="this head has already been pushed")

        rewritten = self._rewrite(dict(zip(specs, bumped, strict=True)))
        if rewritten is None:
            return BumpResult(branch=branch, skipped=f"{_APM_MANIFEST} does not carry the declared spec(s)")
        return self._publish(branch, rewritten, payload)

    def _rewrite(self, replacements: dict[str, str]) -> str | None:
        """The manifest with each declared spec swapped for its bumped form.

        A textual swap of the exact declared strings, so trailing comments and every
        entry the refresher does not own — the third-party pin above all — survive byte
        for byte. ``None`` when any spec is not actually declared: the manifest moved
        since the measurement, and a partial rewrite would open a PR nobody asked for.
        """
        manifest = (self.repo / _APM_MANIFEST).read_text(encoding="utf-8")
        for declared, bumped in replacements.items():
            if declared not in manifest:
                return None
            manifest = manifest.replace(declared, bumped)
        return manifest

    def _remote_branch_exists(self, branch: str) -> bool:
        try:
            listed = run_checked(["git", "ls-remote", "--heads", self.remote, branch], cwd=str(self.repo))
        except CommandFailedError:
            return False
        return bool(listed.stdout.strip())

    def _publish(self, branch: str, manifest: str, payload: ActionPayload) -> BumpResult:
        source = str(payload.get("source", ""))
        head = str(payload.get("head", ""))
        worktree = self.repo.parent / f".{_BRANCH_PREFIX}-{head[:_SHORT_SHA]}"
        base = f"{self.remote}/{_default_branch(self.repo, self.remote)}"
        if not worktree_add_at_ref(str(self.repo), str(worktree), base):
            return BumpResult(branch=branch, skipped=f"could not create a worktree at {base}")
        try:
            run_checked(["git", "checkout", "-q", "-b", branch], cwd=str(worktree))
            (worktree / _APM_MANIFEST).write_text(manifest, encoding="utf-8")
            run_checked(["git", "add", _APM_MANIFEST], cwd=str(worktree))
            run_checked(
                ["git", "commit", "-q", "-m", f"chore(skills): refresh the {source} pin to {head[:_SHORT_SHA]}"],
                cwd=str(worktree),
            )
            run_checked(["git", "push", "-q", self.remote, branch], cwd=str(worktree))
        except CommandFailedError as exc:
            logger.warning("skill_pin_refresh: could not publish %s: %s", branch, exc)
            return BumpResult(branch=branch, skipped="the bump branch could not be pushed")
        finally:
            worktree_remove(str(self.repo), str(worktree))
        pr_url = self.forge.create_pr(branch=branch, title=_title(payload), body=_body(payload))
        return BumpResult(branch=branch, pr_url=pr_url)


def _default_branch(repo: Path, remote: str) -> str:
    try:
        head = run_checked(["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"], cwd=str(repo))
    except CommandFailedError:
        return "main"
    return head.stdout.strip().rsplit("/", 1)[-1] or "main"


def _title(payload: ActionPayload) -> str:
    source = str(payload.get("source", ""))
    return f"chore(skills): refresh the {source} pin to {str(payload.get('head', ''))[:_SHORT_SHA]} (#4677)"


def _body(payload: ActionPayload) -> str:
    log = str(payload.get("log", "")).strip()
    return "\n".join(
        [
            (
                f"`{payload.get('source', '')}` has moved past the pin `apm.yml` declares, so every "
                f"consumer has been reading the pre-drift version."
            ),
            "",
            f"- pinned: `{payload.get('pinned', '')}`",
            f"- source head: `{payload.get('head', '')}`",
            "",
            *(["What the bump brings in:", "", "```", log, "```", ""] if log else []),
            (
                "Takes effect through the existing `t3 setup` install path — no second install "
                "mechanism, and no direct write to `~/.claude/skills`."
            ),
        ]
    )


def open_skill_pin_bump_pr(payload: ActionPayload) -> None:
    """Open the bump PR for one moved source — never raises into the loop."""
    repo = Path(str(payload.get("repo", "")))
    if not repo.is_dir():
        return
    try:
        result = SkillPinBumper(repo=repo, forge=GhForge(repo)).bump(payload)
    except Exception:
        logger.exception("open_skill_pin_bump_pr: could not bump %s", payload.get("source"))
        return
    if result.pr_url:
        _notify(payload, result)


def _notify(payload: ActionPayload, result: BumpResult) -> None:
    try:
        notify_user(
            f"Opened a skill-pin bump PR for {payload.get('source')}: {result.pr_url}\n"
            f"`t3 setup` installs the new pin once it lands.",
            kind=NotifyKind.INFO,
            idempotency_key=f"skill-pin-bump-{result.branch}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("open_skill_pin_bump_pr: could not surface %s", result.pr_url)


__all__ = ["BumpResult", "SkillPinBumper", "open_skill_pin_bump_pr"]
