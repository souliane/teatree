"""Leak-gated fast push (user directive #8): stage → scan → commit → push → PR upsert.

The escape hatch for session hand-offs and token exhaustion: everything the
hook chain runs is skipped EXCEPT the leak gates, which are re-enforced
in-process here — banned terms, the privacy/secret scan, and the
overlay-leak terms + opaque-ID pass all consult the same canonical matchers
and sources as the hook chain (``term_match``, ``resolve_banned_terms``,
``scripts/privacy_scan.py``, ``overlay_leak_terms``), so bypassing the
hooks never bypasses leak protection. Any finding is a hard refusal:
nothing is committed, nothing is pushed.
"""

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from teatree.config import cold_reader
from teatree.hooks.banned_terms_cli import resolve_banned_terms, staged_added_lines
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.opaque_id import find_opaque_ids
from teatree.hooks.term_match import matched_term
from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

LEAK_GATES: Final[tuple[str, str, str]] = ("banned-terms", "secret-scan", "overlay-leak")

_PRIVACY_FINDINGS_EXIT_CODE = 3
_MESSAGE_PATH = "<commit-message>"
_OVERLAY_TERMS_ENV = "TEATREE_OVERLAY_LEAK_TERMS"


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
        result = run_allowed_to_fail(
            ["gh", "pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url"],
            expected_codes=None,
            cwd=self._repo,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        result = run_checked(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
            cwd=self._repo,
        )
        return result.stdout.strip()

    def update_pr(self, *, url: str, body: str) -> None:
        run_checked(["gh", "pr", "edit", url, "--body", body], cwd=self._repo)


class GlabForge:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def find_pr_url(self, *, branch: str) -> str:
        result = run_allowed_to_fail(
            ["glab", "mr", "list", "--source-branch", branch, "-F", "json"],
            expected_codes=None,
            cwd=self._repo,
        )
        if result.returncode != 0:
            return ""
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return ""
        return str(payload[0].get("web_url", "")) if payload else ""

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        result = run_checked(
            ["glab", "mr", "create", "--source-branch", branch, "--title", title, "--description", body, "--yes"],
            cwd=self._repo,
        )
        urls = [token for token in result.stdout.split() if token.startswith("http")]
        return urls[-1] if urls else ""

    def update_pr(self, *, url: str, body: str) -> None:
        run_checked(["glab", "mr", "update", url.rsplit("/", 1)[-1], "--description", body], cwd=self._repo)


def forge_for_repo(repo: Path) -> ForgeClient | None:
    try:
        remote = git.remote_url(repo=str(repo))
    except CommandFailedError:
        return None
    if "github" in remote:
        return GhForge(repo)
    if "gitlab" in remote:
        return GlabForge(repo)
    return None


class LeakGateScan:
    """The three leak gates, run in-process over the staged diff + commit message."""

    def __init__(self, repo: Path, staged_files: list[str], message_text: str) -> None:
        self._repo = repo
        self._files = staged_files
        self._message_text = message_text

    def run(self) -> list[LeakFinding]:
        lines_by_path = self._added_lines_by_path()
        return [
            *self._banned_terms(lines_by_path),
            *self._secret_scan(),
            *self._overlay_leak(lines_by_path),
        ]

    def _added_lines_by_path(self) -> dict[str, list[str]]:
        by_path: dict[str, list[str]] = {}
        for file in self._files:
            added = staged_added_lines(self._repo, file)
            if added is None:
                added = self._full_file_lines(file)
            by_path[file] = added
        by_path[_MESSAGE_PATH] = self._message_text.splitlines()
        return by_path

    def _full_file_lines(self, file: str) -> list[str]:
        try:
            return (self._repo / file).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    @staticmethod
    def _banned_terms(lines_by_path: dict[str, list[str]]) -> list[LeakFinding]:
        try:
            terms = resolve_banned_terms()
        except BannedTermsUnsetError:
            detail = (
                "the banned_terms list is UNSET — fail closed: set T3_BANNED_TERMS or "
                "`t3 <overlay> config_setting set banned_terms '[...]'` (explicit [] opts out)"
            )
            return [LeakFinding(gate="banned-terms", path="", detail=detail)]
        allowlist = tuple(str(t) for t in cold_reader.list_setting("banned_terms_allowlist", default=[]))
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
        diff_text = run_checked(["git", "diff", "--cached"], cwd=self._repo).stdout.rstrip("\n")
        scan_text = f"{diff_text}\n{self._message_text}"
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
        line_paths = _line_paths(diff_text, self._message_text)
        findings = json.loads(result.stdout)
        return [
            LeakFinding(
                gate="secret-scan",
                path=_path_for_line(line_paths, int(item["line"])),
                detail=f"{item['category']}: {item['match']}",
            )
            for item in findings
        ]

    @staticmethod
    def _overlay_leak(lines_by_path: dict[str, list[str]]) -> list[LeakFinding]:
        env = os.environ.get(_OVERLAY_TERMS_ENV, "")
        terms = (
            tuple(t.strip() for t in env.split(",") if t.strip())
            if env
            else tuple(str(t) for t in cold_reader.list_setting("overlay_leak_terms", default=[]))
        )
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


def _line_paths(diff_text: str, message_text: str) -> list[str]:
    paths: list[str] = []
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
        paths.append(current)
    paths.extend(_MESSAGE_PATH for _ in message_text.splitlines())
    return paths


def _path_for_line(line_paths: list[str], line: int) -> str:
    return line_paths[line - 1] if 0 < line <= len(line_paths) else ""


class FastPusher:
    """Sub-minute leak-gated ship: the hook chain is bypassed BECAUSE the gates ran here."""

    def __init__(self, *, repo: Path, message: str = "", remaining: str = "", forge: ForgeClient | None = None) -> None:
        self._repo = repo
        self._message = message
        self._remaining = remaining
        self._forge = forge

    def run(self) -> FastPushOutcome:
        branch = git.current_branch(repo=str(self._repo))
        if branch == self._default_branch():
            detail = f"refusing to fast-push on the default branch '{branch}' — create a feature branch first"
            return FastPushOutcome(
                ok=False,
                branch=branch,
                findings=[LeakFinding(gate="branch-guard", path="", detail=detail)],
            )
        run_checked(["git", "add", "-A"], cwd=self._repo)
        staged = run_checked(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=self._repo,
        ).stdout.splitlines()
        message = self._message or f"chore(wip): fast-push checkpoint ({branch})"
        message_text = f"{message}\n{self._remaining}" if self._remaining else message
        findings = LeakGateScan(self._repo, staged, message_text).run()
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

    def _default_branch(self) -> str:
        try:
            return git.default_branch(repo=str(self._repo))
        except (CommandFailedError, RuntimeError):
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
