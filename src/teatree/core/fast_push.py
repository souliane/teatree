"""Leak-gated fast push (user directive #8): stage → scan → commit → push → PR upsert.

The escape hatch for session hand-offs and token exhaustion: everything the
hook chain runs is skipped EXCEPT the leak gates, which are re-enforced
in-process here so bypassing the hooks never bypasses leak protection. The
four gates mirror the hook chain's four leak checks, consulting the SAME
canonical matchers and sources — banned terms (``term_match`` /
``terms_for_gate("core")``); the privacy/secret scan (``scripts/privacy_scan.py``);
overlay-leak terms + opaque IDs (``overlay_leak_terms`` / ``find_opaque_ids``);
and the public-repo commit-author identity gate (#730). Author/committer email
is commit metadata a diff never shows, so that fourth gate refuses a non-noreply
identity on a PUBLIC GitHub remote exactly as the ``refuse-public-push-with-leak``
pre-push hook does.

The gates run over the PUSH RANGE, not the staged delta. ``git push`` here bypasses the
hook chain, so it delivers every committed-but-unpushed commit on the branch — and three
of the four gates used to read ``git diff --cached`` alone. A secret committed in an
earlier turn, with nothing staged now, was pushed with ``ok: True`` and ``findings: []``
while all four gates reported executed. :mod:`teatree.core.push_range` computes the same
set the bash hook does, including its merge-forward handling, so closing that hole does
not turn every merge-forward push into a refusal.

Any finding is a hard refusal: nothing is committed, nothing is pushed. So is a range
nothing can bound — fail closed, exactly as ``_branch_guard_finding`` already does for an
unresolvable default branch.
"""

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from teatree.core.forge_pr_probe import forge_cli_env, probe_github_open_pr, probe_gitlab_open_pr
from teatree.core.public_identity import is_noreply_email
from teatree.core.push_range import PushRange
from teatree.hooks.banned_term_registry import allowlist_terms, terms_for_gate
from teatree.hooks.banned_terms_cli import staged_added_lines
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.opaque_id import find_opaque_ids
from teatree.hooks.term_match import matched_term
from teatree.utils import git, git_remote
from teatree.utils.forge import forge_from_remote
from teatree.utils.run import run_allowed_to_fail, run_checked

LEAK_GATES: Final[tuple[str, str, str, str]] = (
    "banned-terms",
    "secret-scan",
    "overlay-leak",
    "author-identity",
)

_PRIVACY_FINDINGS_EXIT_CODE = 3
_MESSAGE_PATH = "<commit-message>"
_RANGE_MESSAGE_PATH = "<unpushed-commit-messages>"
_OVERLAY_TERMS_ENV = "TEATREE_OVERLAY_LEAK_TERMS"
_DEFAULT_BRANCH_NAMES: Final[frozenset[str]] = frozenset({"main", "master", "development", "release"})


@dataclass(frozen=True, slots=True)
class LeakFinding:
    gate: str
    path: str
    detail: str


@dataclass(slots=True)
class FastPushOutcome:
    ok: bool
    branch: str
    executed_gates: tuple[str, ...] = ()
    findings: list[LeakFinding] = field(default_factory=list)
    committed: bool = False
    pushed: bool = False
    pr_url: str = ""
    pr_action: str = ""
    message: str = ""


class ForgeClient(Protocol):
    def find_pr_url(self, *, branch: str) -> str: ...  # pragma: no branch

    def create_pr(self, *, branch: str, title: str, body: str) -> str: ...  # pragma: no branch

    def update_pr(self, *, url: str, body: str) -> None: ...  # pragma: no branch


class GhForge:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def find_pr_url(self, *, branch: str) -> str:
        return probe_github_open_pr(self._repo, branch).url_or_empty()

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        result = run_checked(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
            cwd=self._repo,
            env=forge_cli_env(),
        )
        return result.stdout.strip()

    def update_pr(self, *, url: str, body: str) -> None:
        run_checked(["gh", "pr", "edit", url, "--body", body], cwd=self._repo, env=forge_cli_env())


class GlabForge:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def find_pr_url(self, *, branch: str) -> str:
        return probe_gitlab_open_pr(self._repo, branch).url_or_empty()

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        result = run_checked(
            ["glab", "mr", "create", "--source-branch", branch, "--title", title, "--description", body, "--yes"],
            cwd=self._repo,
            env=forge_cli_env(),
        )
        urls = [token for token in result.stdout.split() if token.startswith("http")]
        return urls[-1] if urls else ""

    def update_pr(self, *, url: str, body: str) -> None:
        run_checked(
            ["glab", "mr", "update", url.rsplit("/", 1)[-1], "--description", body],
            cwd=self._repo,
            env=forge_cli_env(),
        )


def forge_for_repo(repo: Path) -> ForgeClient | None:
    kind = forge_from_remote(git.remote_url(repo=str(repo)))
    if kind == "github":
        return GhForge(repo)
    if kind == "gitlab":
        return GlabForge(repo)
    return None


def _public_github_slug(repo: Path) -> str | None:
    """Return the ``owner/repo`` slug when origin is a CONFIRMED-public GitHub remote.

    Mirrors ``refuse-public-push-with-leak.sh``: the identity gate enforces
    only on a public GitHub repo, and fails OPEN (``None``) for a non-GitHub
    remote, an unparsable slug, a missing ``gh``, or any visibility other than
    ``PUBLIC`` (private/internal/unknown) — a private-repo real email is not a
    leak, and blocking every push on a ``gh``-less machine is the over-deny the
    hook deliberately avoids.
    """
    remote = git.remote_url(repo=str(repo))
    if "github" not in remote:
        return None
    slug = git_remote.slug_from_remote(remote)
    if "/" not in slug or shutil.which("gh") is None:
        return None
    result = run_allowed_to_fail(
        ["gh", "repo", "view", slug, "--json", "visibility", "--jq", ".visibility"],
        expected_codes=None,
        cwd=repo,
        env=forge_cli_env(),
    )
    if result.returncode != 0:
        return None
    return slug if result.stdout.strip().upper() == "PUBLIC" else None


def _push_identities(repo: Path, push_range: PushRange) -> list[str]:
    """Author + committer emails that WILL reach the remote on the next push.

    The pending commit's identity (``GIT_AUTHOR_EMAIL`` / ``GIT_COMMITTER_EMAIL``
    override, else ``user.email``) plus every identity in *push_range* — the
    already-committed-but-unpushed commits. Reading the pending identity from
    config keeps the gate PRE-commit, so a bad identity refuses before anything
    is committed.

    The range identities used to be read here behind ``if result.returncode == 0``,
    which silently dropped every unpushed commit's identity whenever the range
    could not be resolved. The read now happens once, in :meth:`PushRange.resolve`,
    and an unresolvable range refuses the push outright instead.
    """
    config_email = git.config_value(repo=str(repo), key="user.email")
    idents = {
        os.environ.get("GIT_AUTHOR_EMAIL", "") or config_email,
        os.environ.get("GIT_COMMITTER_EMAIL", "") or config_email,
        *push_range.identities,
    }
    return sorted(email for email in idents if email)


class LeakGateScan:
    """The four leak gates, over the whole PUSH RANGE plus the staged diff and message.

    The range is what ``git push`` will actually deliver; the staged diff and the
    pending message are what this invocation is about to add to it. Both are in scope
    because either alone leaves a hole: staged-only misses a secret committed in an
    earlier turn, range-only misses the commit this call has not made yet.
    """

    def __init__(self, repo: Path, staged_files: list[str], message_text: str, push_range: PushRange) -> None:
        self._repo = repo
        self._files = staged_files
        self._message_text = message_text
        self._range = push_range

    def run(self) -> list[LeakFinding]:
        lines_by_path = self._added_lines_by_path()
        return [
            *self._banned_terms(lines_by_path),
            *self._secret_scan(),
            *self._overlay_leak(lines_by_path),
            *self._author_identity(),
        ]

    def _author_identity(self) -> list[LeakFinding]:
        slug = _public_github_slug(self._repo)
        if slug is None:
            return []
        return [
            LeakFinding(
                gate="author-identity",
                path="<commit-identity>",
                detail=f"non-noreply commit identity '{email}' would leak to public repo {slug}",
            )
            for email in _push_identities(self._repo, self._range)
            if not is_noreply_email(email)
        ]

    def _added_lines_by_path(self) -> dict[str, list[str]]:
        by_path: dict[str, list[str]] = {}
        for file in self._files:
            added = staged_added_lines(self._repo, file)
            if added is None:
                added = self._full_file_lines(file)
            by_path[file] = added
        for path, lines in _added_lines_from_diff(self._range.diff_text).items():
            by_path.setdefault(path, []).extend(lines)
        by_path[_MESSAGE_PATH] = self._message_text.splitlines()
        by_path[_RANGE_MESSAGE_PATH] = self._range.commit_messages.splitlines()
        return by_path

    def _full_file_lines(self, file: str) -> list[str]:
        try:
            return (self._repo / file).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    @staticmethod
    def _banned_terms(lines_by_path: dict[str, list[str]]) -> list[LeakFinding]:
        try:
            terms = terms_for_gate("core")
        except BannedTermsUnsetError:
            detail = (
                "the banned_terms list is UNSET — fail closed: set T3_BANNED_TERMS or "
                "`t3 <overlay> config_setting set banned_terms '[...]'` (explicit [] opts out)"
            )
            return [LeakFinding(gate="banned-terms", path="", detail=detail)]
        allowlist = allowlist_terms()
        return [
            LeakFinding(gate="banned-terms", path=path, detail=f"banned term '{term}'")
            for path, lines in lines_by_path.items()
            for line in lines
            if (term := matched_term(line, terms, allowlist))
        ]

    def _secret_scan(self) -> list[LeakFinding]:
        script = _privacy_scan_script()
        if script is None:
            return [LeakFinding(gate="secret-scan", path="", detail="scripts/privacy_scan.py not found — fail closed")]
        scanned = self._scan_lines()
        scan_text = "\n".join(line for line, _ in scanned)
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as handle:
            handle.write(scan_text)
            scan_path = Path(handle.name)
        try:
            result = run_allowed_to_fail(
                [sys.executable, str(script), str(scan_path), "--json"],
                expected_codes=None,
            )
        finally:
            scan_path.unlink(missing_ok=True)
        if result.returncode == 0:
            return []
        if result.returncode != _PRIVACY_FINDINGS_EXIT_CODE:
            detail = f"privacy scanner could not run (exit {result.returncode}) — fail closed"
            return [LeakFinding(gate="secret-scan", path="", detail=detail)]
        findings = json.loads(result.stdout)
        return [
            LeakFinding(
                gate="secret-scan",
                path=_path_for_line(scanned, int(item["line"])),
                detail=f"{item['category']}: {item['match']}",
            )
            for item in findings
        ]

    def _scan_lines(self) -> list[tuple[str, str]]:
        """``(line, owning path)`` for every line the scanner sees, in scan order.

        The scanner reports findings by LINE NUMBER into the concatenated blob, so the
        path column has to be built from the same concatenation — one pass, never two
        that can drift apart.
        """
        staged_diff = run_checked(["git", "diff", "--cached"], cwd=self._repo).stdout.rstrip("\n")
        pairs: list[tuple[str, str]] = []
        for diff_text in (self._range.diff_text, staged_diff):
            pairs.extend(zip(diff_text.splitlines(), _diff_line_paths(diff_text), strict=True))
        pairs.extend((line, _MESSAGE_PATH) for line in self._message_text.splitlines())
        pairs.extend((line, _RANGE_MESSAGE_PATH) for line in self._range.commit_messages.splitlines())
        return pairs

    @staticmethod
    def _overlay_leak(lines_by_path: dict[str, list[str]]) -> list[LeakFinding]:
        env = os.environ.get(_OVERLAY_TERMS_ENV, "")
        # Registry-first dual-read via terms_for_gate("overlay"); the overlay env
        # override still WINS, matching check_no_overlay_leak.
        terms = tuple(t.strip() for t in env.split(",") if t.strip()) if env else terms_for_gate("overlay")
        findings = [
            LeakFinding(gate="overlay-leak", path=path, detail=f"overlay-scoped term '{term}'")
            for path, lines in lines_by_path.items()
            for line in lines
            if (term := matched_term(line, terms))
        ]
        findings.extend(
            LeakFinding(gate="overlay-leak", path=path, detail=f"opaque id '{opaque}'")
            for path, lines in lines_by_path.items()
            for line in lines
            for opaque in find_opaque_ids(line)
        )
        return findings


def _privacy_scan_script() -> Path | None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "privacy_scan.py"
    return script if script.is_file() else None


def _diff_line_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
        paths.append(current)
    return paths


def _path_for_line(scanned: list[tuple[str, str]], line: int) -> str:
    return scanned[line - 1][1] if 0 < line <= len(scanned) else ""


def _added_lines_from_diff(diff_text: str) -> dict[str, list[str]]:
    """``{path: added line bodies}`` from a unified diff.

    HUNK-AWARE, for the reason :func:`staged_added_lines` is: git renders an added line
    whose own text starts with ``++`` as ``+++text``, so a naive
    ``not line.startswith("+++")`` filter drops it — and a banned term on such a line
    would slip the gate. File headers only ever appear BEFORE the first ``@@``.
    """
    by_path: dict[str, list[str]] = {}
    path = ""
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = "", False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
        elif in_hunk and line.startswith("+") and path:
            by_path.setdefault(path, []).append(line[1:])
    return by_path


class FastPusher:
    """Sub-minute leak-gated ship: the hook chain is bypassed BECAUSE the gates ran here."""

    def __init__(self, *, repo: Path, message: str = "", remaining: str = "", forge: ForgeClient | None = None) -> None:
        self._repo = repo
        self._message = message
        self._remaining = remaining
        self._forge = forge

    def run(self) -> FastPushOutcome:
        branch = git.current_branch(repo=str(self._repo))
        guard = self._branch_guard_finding(branch)
        if guard is not None:
            return FastPushOutcome(ok=False, branch=branch, findings=[guard])
        push_range = PushRange.resolve(self._repo, branch=branch, default_branch=self._default_branch())
        if push_range is None:
            detail = (
                "refusing to fast-push: could not resolve what this push would newly expose "
                f"(no reachable origin/{self._default_branch()} tip) — fail closed"
            )
            return FastPushOutcome(ok=False, branch=branch, findings=[LeakFinding("push-range", "", detail)])
        run_checked(["git", "add", "-A"], cwd=self._repo)
        staged = run_checked(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=self._repo,
        ).stdout.splitlines()
        message = self._message or f"chore(wip): fast-push checkpoint ({branch})"
        message_text = f"{message}\n{self._remaining}" if self._remaining else message
        findings = LeakGateScan(self._repo, staged, message_text, push_range).run()
        if findings:
            return FastPushOutcome(ok=False, branch=branch, executed_gates=LEAK_GATES, findings=findings)
        outcome = FastPushOutcome(ok=True, branch=branch, executed_gates=LEAK_GATES, message=message)
        if staged:
            self._commit(message)
            outcome.committed = True
        run_checked(["git", "push", "--no-verify", "-u", "origin", branch], cwd=self._repo)
        outcome.pushed = True
        self._upsert_pr(outcome)
        return outcome

    def _branch_guard_finding(self, branch: str) -> LeakFinding | None:
        """Refuse a push onto (or that cannot be proven to be OFF) the default branch.

        Fails CLOSED: an unresolvable default branch refuses rather than
        letting ``--no-verify`` slip a checkpoint onto ``main`` — the
        opposite of a fail-open guard.
        """
        if branch in _DEFAULT_BRANCH_NAMES:
            detail = f"refusing to fast-push on the default branch '{branch}' — create a feature branch first"
            return LeakFinding(gate="branch-guard", path="", detail=detail)
        default = self._default_branch()
        if not default:
            detail = "refusing to fast-push: could not resolve the default branch (fail closed)"
            return LeakFinding(gate="branch-guard", path="", detail=detail)
        if branch == default:
            detail = f"refusing to fast-push on the default branch '{branch}' — create a feature branch first"
            return LeakFinding(gate="branch-guard", path="", detail=detail)
        return None

    def _default_branch(self) -> str:
        try:
            return git.default_branch(repo=str(self._repo))
        except (RuntimeError, ValueError):
            return ""

    def _commit(self, message: str) -> None:
        cmd = ["git", "commit", "--no-verify", "-m", message]
        if self._remaining:
            cmd.extend(["-m", f"REMAINING:\n{self._remaining}"])
        run_checked(cmd, cwd=self._repo)

    def _pr_body(self, message: str) -> str:
        body = message
        if self._remaining:
            body = f"{body}\n\nREMAINING:\n{self._remaining}"
        return body

    def _upsert_pr(self, outcome: FastPushOutcome) -> None:
        forge = self._forge or forge_for_repo(self._repo)
        if forge is None:
            outcome.pr_action = "skipped"
            return
        body = self._pr_body(outcome.message)
        existing = forge.find_pr_url(branch=outcome.branch)
        if existing:
            forge.update_pr(url=existing, body=body)
            outcome.pr_url = existing
            outcome.pr_action = "updated"
            return
        title = outcome.message.splitlines()[0]
        outcome.pr_url = forge.create_pr(branch=outcome.branch, title=title, body=body)
        outcome.pr_action = "created"
