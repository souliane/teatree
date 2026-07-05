# test-path: cross-cutting — drives every PreToolUse gate in hook_router.py (hooks/); no src/teatree/ mirror.
"""Gate-liveness / enforcement-conformance corpus.

The symmetric companion to ``test_lockout_regression_corpus.py``. That corpus
catches OVER-deny (a gate that locks the factory out by denying a legitimate
command). This corpus catches UNDER-fire (a gate whose handler is correct but
that never fires on real input) and UNREACHABLE gates (a handler keyed on a
tool/skill that no registered ``hooks.json`` matcher ever delivers to its
event — a *phantom* gate, #167/#171).

Every deny/enforcement gate is one :class:`GateRow`. Three mechanical
assertions run per row:

(a) DENIES the row's real must-DENY payload;
(b) ALLOWS the row's real must-ALLOW payload;
(c) REACHABILITY — the gate's declared ``matched`` tool/skill is actually
delivered to the handler's ``event`` by a registered ``hooks.json`` matcher
(PreToolUse: the tool name matches a matcher regex; TaskCreated/Stop: a handler
is registered on that event).

After #1646 wired the ``Agent`` PreToolUse matcher, NO reachability phantoms
remain. The two PreToolUse ``Agent`` arms — the dispatch-quote and
orchestrator-boundary gates — are now genuinely live (the ``Agent`` tool reaches
PreToolUse and a registered ``Agent`` matcher delivers it). The
orchestrator-boundary foreground-Agent guard is additionally default-ON (#1733),
with its never-lockout off-ramps intact (sub-agent context,
``run_in_background: true``, ``[fg-ok: <reason>]`` token, kill-switch,
deny-circuit-breaker, and ``_fail_open_or_deny`` routing #1692). Distinguish the
PreToolUse ``Agent`` path from the ``Task``/``Workflow`` fan-out vehicle, which
genuinely DOES bypass PreToolUse and fires ``TaskCreated`` instead (no
``run_in_background`` in its schema) — that is why the dispatch-quote concern
also carries a reachable ``TaskCreated`` counterpart.
The phantom roster is now asserted EMPTY (also visible via ``-rsx``); a row
silently gaining phantom status without a deliberate update FAILS the build.
"""

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.pretooluse_verdict import Verdict
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.hooks import _repo_visibility

# ── environment & invocation context ────────────────────────────────────

_HOOKS_JSON: Final[Path] = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
_REPO_SKILLS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "skills"


@dataclass
class GateContext:
    """Per-test arranged environment handed to each row's payload builders."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    home: Path
    state_dir: Path
    session_id: str = "sess-liveness"

    def write_teatree_toml(self, body: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".teatree.toml").write_text(body, encoding="utf-8")

    def patch_t3_subprocess(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        """Pin ``shutil.which('t3')`` and the gate's shelled validator result.

        Gates that shell ``t3 tool …`` (AI-sig, MR-metadata) are made
        deterministic without a real ``t3`` on PATH: ``which`` resolves and
        ``subprocess.run`` returns a fixed :class:`CompletedProcess`.
        """
        self.monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
        self.monkeypatch.setattr(router.subprocess, "run", lambda *a, **k: result)

    def write_state(self, suffix: str, lines: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / f"{self.session_id}.{suffix}").write_text(lines, encoding="utf-8")


PayloadBuilder = Callable[[GateContext], dict]
Arranger = Callable[[GateContext], None]


@dataclass(frozen=True)
class GateRow:
    """One deny/enforcement gate. Adding a gate is exactly one of these rows."""

    gate_id: str
    handler: Callable[[dict], bool | Verdict | None]
    event: str
    matched: str
    deny_input: PayloadBuilder
    allow_input: PayloadBuilder
    arrange: Arranger = field(default=lambda _ctx: None)
    # A phantom gate fails reachability (c). ``phantom_reason`` is the xfail
    # text for (c). ``allow_phantom_reason`` additionally marks (b) xfail when a
    # real must-ALLOW payload cannot clear the gate. No row currently sets it
    # (the #167 plan-tracker mismatch that needed it is fixed), but the
    # mechanism stays for a future gate whose allow-path is genuinely blocked.
    phantom_reason: str | None = None
    allow_phantom_reason: str | None = None


# ── reachability: parse hooks.json matchers ──────────────────────────────


def _registered_matchers(event: str) -> list[str]:
    """Return the matcher strings registered for *event* in ``hooks.json``.

    An entry with no ``matcher`` key (TaskCreated/Stop/…) contributes the
    empty string, signalling "every tool on this event reaches the handler".
    """
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    entries = config.get("hooks", {}).get(event, [])
    return [entry.get("matcher", "") for entry in entries]


def _tool_is_routed(event: str, tool_name: str) -> bool:
    """True iff *tool_name* is delivered to *event*'s handler chain.

    A matcher is an alternation regex (``Bash|Edit|Write``) anchored to the
    full tool name. An empty matcher (eventless registration) routes every
    tool. Mirrors how the Claude Code harness selects PreToolUse hooks.
    """
    matchers = _registered_matchers(event)
    if not matchers:
        return False
    for matcher in matchers:
        if matcher == "":
            return True
        if re.fullmatch(matcher, tool_name):
            return True
    return False


def _handler_registered(event: str, handler: Callable) -> bool:
    return handler in router._HANDLERS.get(event, [])


def _gate_is_reachable(row: GateRow) -> bool:
    """Assertion (c): is the gate's ``matched`` token actually delivered?

    PreToolUse: the matched tool must match a registered matcher regex AND the
    handler must be in the PreToolUse chain. TaskCreated/Stop and other
    eventless registrations: a handler registered on that event reaches every
    tool, so reachability reduces to "is the handler registered on its event".
    """
    if not _handler_registered(row.event, row.handler):
        return False
    if row.event == "PreToolUse":
        return _tool_is_routed(row.event, row.matched)
    return True


# ── deny detection ───────────────────────────────────────────────────────


def _denied(handler: Callable[[dict], bool | Verdict | None], event_input: dict) -> bool:
    """Run *handler*; True iff it denied (returned ``True``).

    Every deny gate in scope signals a deny via a ``True`` return — the
    PreToolUse ``hookSpecificOutput`` deny, the TaskCreated ``continue: false``
    envelope, and the Stop ``systemMessage`` break all return ``True`` from
    their handler. A ``None``/``False`` return is an allow (pass-through).
    """
    return handler(event_input) is True


# ── payload builders ──────────────────────────────────────────────────────
#
# Synthetic names only (public repo): ``acme`` / ``t3-acme`` /
# ``attacker-org/acme-product`` / ``overlay-a:``. Never a real
# tenant/overlay/colleague.

_HIGH_QUOTE = "Here is what the user said: ship it now."  # the-user-said-colon HIGH
_BANNED_TERM_TOML = '[teatree]\nbanned_terms = ["acme"]\n'
_BANNED_BODY = "Rolling out the acme integration."
_AI_SIG_TRAILER = "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"


def _bash(command: str) -> dict:
    return {"session_id": "sess-liveness", "tool_name": "Bash", "tool_input": {"command": command}}


def _slack_send(tool: str, text: str) -> dict:
    return {"session_id": "sess-liveness", "tool_name": tool, "tool_input": {"text": text}}


def _agent(prompt: str, *, run_in_background: bool = False) -> dict:
    return {
        "session_id": "sess-liveness",
        "tool_name": "Agent",
        "tool_input": {"prompt": prompt, "run_in_background": run_in_background},
    }


# skill-loading (PreToolUse): a real resolvable skill in <session>.pending
# that is not in <session>.skills must block Bash; the [skill-load-ok] token
# (or having loaded it) clears the gate.


def _arrange_skill_loading(ctx: GateContext) -> None:
    ctx.monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_REPO_SKILLS_DIR))
    ctx.write_state("pending", "code\n")
    ctx.write_state("skills", "")


def _arrange_skill_loading_on_task(ctx: GateContext) -> None:
    ctx.monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_REPO_SKILLS_DIR))
    ctx.write_state("pending", "code\n")
    ctx.write_state("skills", "")


def _task_created(description: str, *, skip: bool) -> dict:
    token = "[skip-skill-gate: false-trigger] " if skip else ""
    return {
        "session_id": "sess-liveness",
        "task_subject": "do the thing",
        "task_description": f"{token}{description}",
    }


# block-edit-before-planned (PreToolUse Edit/Write): deny Edit/Write when the
# worktree's ticket is still in STARTED state (no PlanArtifact yet).
# _ticket_state_for_cwd() resolves via Django/DB, so the corpus monkeypatches it
# directly rather than spinning up Django.


def _arrange_block_edit_before_planned(ctx: GateContext) -> None:
    ctx.monkeypatch.setattr(router, "_ticket_state_for_cwd", lambda _cwd: "started")


def _block_edit_before_planned_deny(ctx: GateContext) -> dict:
    return {
        "session_id": ctx.session_id,
        "tool_name": "Edit",
        "cwd": str(ctx.tmp_path),
        "tool_input": {"file_path": str(ctx.tmp_path / "module.py"), "old_string": "a", "new_string": "b"},
    }


def _block_edit_before_planned_allow(ctx: GateContext) -> dict:
    ctx.monkeypatch.setattr(router, "_ticket_state_for_cwd", lambda _cwd: "planned")
    return {
        "session_id": ctx.session_id,
        "tool_name": "Edit",
        "cwd": str(ctx.tmp_path),
        "tool_input": {"file_path": str(ctx.tmp_path / "module.py"), "old_string": "a", "new_string": "b"},
    }


# block-config-overwrite (PreToolUse Write/Edit/Bash): a Write that overwrites an
# existing config/dotfile NOT read this session must block; recording the path in
# <session>.reads first clears it.


def _config_overwrite_cfg(ctx: GateContext) -> Path:
    cfg = ctx.tmp_path / ".teatree.toml"
    cfg.write_text("old = true\n", encoding="utf-8")
    return cfg


def _block_config_overwrite_deny(ctx: GateContext) -> dict:
    cfg = _config_overwrite_cfg(ctx)
    return {
        "session_id": ctx.session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": str(cfg), "content": "new = true\n"},
    }


def _block_config_overwrite_allow(ctx: GateContext) -> dict:
    cfg = _config_overwrite_cfg(ctx)
    ctx.write_state("reads", f"0.0\t{cfg}\n")
    return {
        "session_id": ctx.session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": str(cfg), "content": "new = true\n"},
    }


# protect-default-branch (PreToolUse Edit/Write/Read): an Edit on a file in a
# teatree-managed repo checked out on main must block; a worktree branch allows.


def _git(cwd: Path, *args: str) -> None:
    import os  # noqa: PLC0415

    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _init_repo(repo: Path, branch: str, remote_slug: str) -> None:
    """Init *repo* on *branch* with one commit so ``rev-parse HEAD`` resolves.

    A repo with no commit fails ``rev-parse --abbrev-ref HEAD`` (exit 128), so
    ``_resolve_branch_and_root`` would fail open and the branch gate never
    fires — defeating the test's premise. The seed commit makes the branch real.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", branch)
    _git(repo, "remote", "add", "origin", f"git@github.com:{remote_slug}.git")
    (repo / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "seed")


def _managed_repo(ctx: GateContext, branch: str) -> Path:
    ctx.write_teatree_toml('[overlays.acme]\nworkspace_repos = ["attacker-org/acme-product"]\n')
    repo = ctx.tmp_path / "acme-product"
    _init_repo(repo, branch, "attacker-org/acme-product")
    return repo


def _protect_branch_deny(ctx: GateContext) -> dict:
    repo = _managed_repo(ctx, "main")
    return {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "module.py")}}


def _protect_branch_allow(ctx: GateContext) -> dict:
    repo = _managed_repo(ctx, "1-feat-acme")
    return {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "module.py")}}


# block-main-clone-mutation (PreToolUse Bash): a `git checkout <feature>` run in
# a teatree-managed MAIN CLONE (a `.git`-*dir* primary clone) is denied; a
# read-only `git status` in the same clone passes through (#2836).


def _main_clone_bash_deny(ctx: GateContext) -> dict:
    repo = _managed_repo(ctx, "main")
    return {
        "session_id": ctx.session_id,
        "tool_name": "Bash",
        "tool_input": {"command": "git checkout feature"},
        "cwd": str(repo),
    }


def _main_clone_bash_allow(ctx: GateContext) -> dict:
    repo = _managed_repo(ctx, "main")
    return {
        "session_id": ctx.session_id,
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": str(repo),
    }


# validate-mr-metadata Bash arm (PreToolUse Bash): glab mr create routes to the
# overlay validator; rc!=0 denies, rc==0 allows.


def _mr_meta_bash_deny(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=1, stderr="bad title")
    return _bash("glab mr create --title '' --description ''")


def _mr_meta_bash_allow(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=0)
    return _bash("glab mr create --title 'feat: add acme widget' --description 'Closes #4242'")


# validate-mr-metadata MCP arm (mcp__glab__glab_mr_create) — handler validates,
# but the MCP tool is NOT in any PreToolUse matcher (phantom).


def _mr_meta_mcp_deny(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=1, stderr="bad title")
    return {"tool_name": "mcp__glab__glab_mr_create", "tool_input": {"title": "", "description": ""}}


def _mr_meta_mcp_allow(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=0)
    return {
        "tool_name": "mcp__glab__glab_mr_create",
        "tool_input": {"title": "feat: add acme widget", "description": "Closes #4242"},
    }


# block-self-reviewer-assign (PreToolUse): a reviewer-assignment surface denies;
# a metadata-only edit / a GET read of the reviewer list allows. The gate
# decides purely from the command — no t3 subprocess.


def _reviewer_assign_bash_deny(ctx: GateContext) -> dict:
    return _bash("glab mr update 7624 --reviewer WouterLachat")


def _reviewer_assign_bash_allow(ctx: GateContext) -> dict:
    return _bash("glab mr update 12 --add-label needs-review")


def _reviewer_assign_mcp_deny(ctx: GateContext) -> dict:
    return {"tool_name": "mcp__glab__glab_mr_update", "tool_input": {"iid": 7624, "reviewer": "WouterLachat"}}


def _reviewer_assign_mcp_allow(ctx: GateContext) -> dict:
    return {"tool_name": "mcp__glab__glab_mr_update", "tool_input": {"iid": 7624, "title": "fix: x (proj#1)"}}


# block-ai-signature (PreToolUse Bash): a commit carrying a banned trailer
# routes to the AI-sig scanner; rc!=0 denies, a clean commit (no payload) allows.


def _ai_sig_deny(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=1, stdout="banned trailer")
    return _bash(f"git commit -m '{_AI_SIG_TRAILER}'")


def _ai_sig_allow(ctx: GateContext) -> dict:
    ctx.patch_t3_subprocess(returncode=0)
    return _bash("git commit -m 'fix: tidy up'")


# quote-scanner (PreToolUse Bash arm): a publish command whose body carries a
# verbatim user quote denies; a clean body allows.


# The leak gates (#1415/#1213) enforce ONLY on an affirmatively-PUBLIC target, so
# the must-DENY rows post to the genuinely-public ``souliane/teatree`` with the
# probe pinned public (and a cold visibility cache) for a deterministic fire.
def _pin_public_probe(ctx: GateContext) -> None:
    ctx.monkeypatch.setenv("T3_DATA_DIR", str(ctx.tmp_path / "viscache"))
    ctx.monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")


def _quote_bash_deny(ctx: GateContext) -> dict:
    return _bash(f'gh issue create --repo souliane/teatree --title t --body "{_HIGH_QUOTE}"')


def _quote_bash_allow(ctx: GateContext) -> dict:
    return _bash('gh issue create --repo souliane/teatree --title t --body "Routine status update."')


# quote-scanner Slack-MCP arm (mcp__*slack* send) — now reachable via the
# ``mcp__.*[Ss]lack.*`` PreToolUse matcher (#171). The arm is default-ON; the
# corpus enables ``mcp_privacy_gate_enabled`` explicitly so the must-DENY fires
# regardless of any developer-local config leaking through.


def _arrange_mcp_privacy_gate(ctx: GateContext) -> None:
    ctx.write_teatree_toml("[teatree]\nmcp_privacy_gate_enabled = true\n")


def _quote_slack_deny(ctx: GateContext) -> dict:
    return _slack_send("mcp__claude_ai_Slack__slack_send_message", _HIGH_QUOTE)


def _quote_slack_allow(ctx: GateContext) -> dict:
    return _slack_send("mcp__claude_ai_Slack__slack_send_message", "Routine status update.")


# self-DM gate (mcp__*slack* send/react): a write to a configured bot↔user DM
# channel denies (renders as user-authored under the personal token); a write to
# a colleague channel allows. The arrange step declares the DM channel id under
# an overlay table so the gate can resolve it.

_SELF_DM_CHANNEL = "D0BLIVEDM001"


def _arrange_self_dm_gate(ctx: GateContext) -> None:
    ctx.write_teatree_toml(f'[overlays.t3-acme]\nslack_dm_channel_id = "{_SELF_DM_CHANNEL}"\n')


def _self_dm_deny(ctx: GateContext) -> dict:
    return {
        "session_id": "sess-liveness",
        "tool_name": "mcp__claude_ai_Slack__slack_send_message",
        "tool_input": {"channel": _SELF_DM_CHANNEL, "text": "Full-day review report"},
    }


def _self_dm_allow(ctx: GateContext) -> dict:
    return {
        "session_id": "sess-liveness",
        "tool_name": "mcp__claude_ai_Slack__slack_send_message",
        "tool_input": {"channel": "C0COLLEAGUE9", "text": "review note"},
    }


# block-mcp-slack-write (#1196): a Slack MCP WRITE (any destination) denies —
# every Slack write must route through the t3 CLI; a Slack MCP READ allows.
def _mcp_slack_write_deny(ctx: GateContext) -> dict:
    return {
        "session_id": "sess-liveness",
        "tool_name": "mcp__claude_ai_Slack__slack_send_message",
        "tool_input": {"channel": "C0COLLEAGUE9", "text": "review note"},
    }


def _mcp_slack_write_allow(ctx: GateContext) -> dict:
    return {
        "session_id": "sess-liveness",
        "tool_name": "mcp__claude_ai_Slack__slack_get_channel_history",
        "tool_input": {"channel": "C0COLLEAGUE9"},
    }


# dispatch-prompt quote-scanner (Agent/Task): a dispatch prompt carrying a
# verbatim user quote denies; a clean prompt allows. Now REACHABLE — #1646 wired
# the `Agent` PreToolUse matcher in hooks.json. The clean-prompt fan-out concern
# is also covered by the reachable TaskCreated counterpart below.


def _dispatch_quote_deny(ctx: GateContext) -> dict:
    return {"session_id": ctx.session_id, "tool_name": "Agent", "tool_input": {"prompt": _HIGH_QUOTE}}


def _dispatch_quote_allow(ctx: GateContext) -> dict:
    return {
        "session_id": ctx.session_id,
        "tool_name": "Agent",
        "tool_input": {"prompt": "Implement the acme widget per the spec."},
    }


# dispatch-prompt quote-scanner ON TaskCreated (the fan-out arm, #171): the
# fan-out path bypasses PreToolUse, so this TaskCreated handler scans the
# task subject/description. Ships default-OFF (opt-in pending #1640-class
# fan-out validation), so the corpus enables it explicitly to prove it CAN
# fire when on: reachable + denies a HIGH-quote fan-out + allows a clean one.


def _arrange_dispatch_quote_on_task(ctx: GateContext) -> None:
    ctx.write_teatree_toml("[teatree]\ndispatch_quote_gate_on_task_create_enabled = true\n")


def _dispatch_quote_task_deny(ctx: GateContext) -> dict:
    return {"session_id": ctx.session_id, "task_subject": "do work", "task_description": _HIGH_QUOTE}


def _dispatch_quote_task_allow(ctx: GateContext) -> dict:
    return {
        "session_id": ctx.session_id,
        "task_subject": "do work",
        "task_description": "Implement the acme widget per the spec.",
    }


# banned-terms (PreToolUse Bash arm): a publish body carrying a configured
# banned term denies; a clean body allows. (No Slack-MCP arm exists.)


def _arrange_banned_terms(ctx: GateContext) -> None:
    cfg = ctx.tmp_path / "banned.toml"
    cfg.write_text(_BANNED_TERM_TOML, encoding="utf-8")
    ctx.monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(cfg))
    ctx.monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
    _pin_public_probe(ctx)


def _banned_bash_deny(ctx: GateContext) -> dict:
    return _bash(f'gh issue create --repo souliane/teatree --title t --body "{_BANNED_BODY}"')


def _banned_bash_allow(ctx: GateContext) -> dict:
    return _bash('gh issue create --repo souliane/teatree --title t --body "Rolling out the integration."')


# block-uncovered-diff (PreToolUse Bash): a non-draft gh pr create whose diff
# fails Gate 12 denies; a passing report allows.


def _uncovered_deny(ctx: GateContext) -> dict:
    report = json.dumps({"passes": False, "uncovered": [{"path": "a.py", "lines": [1, 2]}]})
    ctx.patch_t3_subprocess(returncode=1, stdout=report)
    return _bash("gh pr create --title t --body b")


def _uncovered_allow(ctx: GateContext) -> dict:
    report = json.dumps({"passes": True, "uncovered": []})
    ctx.patch_t3_subprocess(returncode=0, stdout=report)
    return _bash("gh pr create --title t --body b")


# enforce-orchestrator-boundary Bash arm (PreToolUse Bash): a foreground heavy
# Bash command from the main agent denies; run_in_background clears it.


def _orch_bash_deny(ctx: GateContext) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": "pytest tests/", "run_in_background": False}}


def _orch_bash_allow(ctx: GateContext) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": "pytest tests/", "run_in_background": True}}


# enforce-orchestrator-boundary Agent arm (#1442): a foreground Agent dispatch
# from the main agent denies. Now REACHABLE (#1646 wired the `Agent` PreToolUse
# matcher) and default-ON (#1733). The arrange writes the flag explicitly — a
# no-op for the verdict since it is default-ON, but it documents intent at the
# call site. run_in_background / a [fg-ok: <reason>] token / a sub-agent context
# clears it; the deny routes through _fail_open_or_deny (#1692).


def _arrange_orch_agent_gate(ctx: GateContext) -> None:
    ctx.write_teatree_toml("[teatree]\norchestrator_boundary_agent_gate_enabled = true\n")


def _orch_agent_deny(ctx: GateContext) -> dict:
    return _agent("implement", run_in_background=False)


def _orch_agent_allow(ctx: GateContext) -> dict:
    return _agent("implement", run_in_background=True)


# block-direct-commands (PreToolUse Bash): a blocked tool invocation denies; a
# t3 / read-only command allows.


def _direct_deny(_ctx: GateContext) -> dict:
    return _bash("pip install requests")


def _direct_allow(_ctx: GateContext) -> dict:
    return _bash("t3 teatree ticket list")


# block-out-of-band-merge (PreToolUse Bash): a raw merge in a managed repo
# denies; the same merge in an unmanaged repo allows.


def _oob_merge_deny(ctx: GateContext) -> dict:
    repo = _managed_repo(ctx, "main")
    return {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(repo)}


def _oob_merge_allow(ctx: GateContext) -> dict:
    ctx.write_teatree_toml('[overlays.acme]\nworkspace_repos = ["attacker-org/acme-product"]\n')
    repo = ctx.tmp_path / "unmanaged"
    _init_repo(repo, "main", "someone-else/public")
    return {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(repo)}


# block-unknown-repo-push (PreToolUse Bash): a ``git push`` to a repo NO
# registered overlay owns HOLDS for approval; a push to an OWNED repo allows.
# The gate ships INERT (``require_owned_repo_approval`` defaults False), so the
# corpus injects an opted-in overlay set (``owned_repos={"github.com":
# ["souliane"]}``, flag True) to exercise the gate LOGIC. ``souliane/teatree``
# is then owned; ``randomuser/randomrepo`` is unknown.


class _OptedInScopeOverlay(OverlayBase):
    def __init__(self) -> None:
        self.config = OverlayConfig()
        self.config.owned_repos = {"github.com": ["souliane"]}
        self.config.require_owned_repo_approval = True

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: object) -> list[object]:  # type: ignore[override]
        _ = worktree
        return []


def _opt_in_scope_gate(ctx: GateContext) -> None:
    ctx.monkeypatch.setattr(
        "teatree.core.overlay_loader.get_all_overlays",
        lambda: {"t3-teatree": _OptedInScopeOverlay()},
    )


def _unknown_push_deny(ctx: GateContext) -> dict:
    _opt_in_scope_gate(ctx)
    repo = ctx.tmp_path / "unknown-target"
    _init_repo(repo, "main", "randomuser/randomrepo")
    return {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "cwd": str(repo)}


def _unknown_push_allow(ctx: GateContext) -> dict:
    _opt_in_scope_gate(ctx)
    repo = ctx.tmp_path / "owned-target"
    _init_repo(repo, "main", "souliane/teatree")
    return {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "cwd": str(repo)}


# block-raw-review-post (PreToolUse Bash): a raw forge REST WRITE to a review
# endpoint denies; a bare GET read allows.


def _raw_review_deny(_ctx: GateContext) -> dict:
    return _bash("glab api projects/1/merge_requests/1/discussions -X POST -f body=lgtm")


def _raw_review_allow(_ctx: GateContext) -> dict:
    return _bash("glab api projects/1/merge_requests/1/discussions")


# block-raw-pid-kill (PreToolUse Bash): a raw `kill <pid>` of a guessed pid
# denies; the `kill -0` no-op liveness probe allows.


def _raw_pid_kill_deny(_ctx: GateContext) -> dict:
    return _bash("kill -9 4242")


def _raw_pid_kill_allow(_ctx: GateContext) -> dict:
    return _bash("kill -0 4242")


# block-secret-file-print (PreToolUse Bash): printing a credential file to the
# transcript denies; capturing the value into a variable allows.


def _secret_print_deny(_ctx: GateContext) -> dict:
    return _bash("cat ~/.teatree.toml")


def _secret_print_allow(_ctx: GateContext) -> dict:
    return _bash("TOKEN=$(pass show infra/api-key)")


# classifier-deny stop gate (Stop): a pending classifier-deny marker emits the
# STOP-and-explain systemMessage; no marker allows the Stop chain to proceed.


def _classifier_stop_deny(ctx: GateContext) -> dict:
    ctx.write_state("classifier-deny", json.dumps({"tool_name": "Bash", "action": "git push"}))
    return {"session_id": ctx.session_id}


def _classifier_stop_allow(ctx: GateContext) -> dict:
    return {"session_id": ctx.session_id}


# ── the registry ──────────────────────────────────────────────────────────

# The two PreToolUse `Agent` arms (`dispatch-prompt-quote-scanner` and
# `enforce-orchestrator-boundary-agent`) are now REACHABLE: #1646 wired the
# `Agent` PreToolUse matcher in hooks.json (the registered PreToolUse matchers
# are `Bash|Edit|Write`, `AskUserQuestion`, `mcp__.*[Ss]lack.*`,
# `mcp__glab__glab_mr_.*`, `Agent`). The orchestrator-boundary Agent deny is
# additionally default-ON (#1733). The SEPARATE `Task`/`Workflow` fan-out vehicle
# still bypasses PreToolUse and fires TaskCreated (no `run_in_background` in its
# schema; verified against the Claude Code binary, docs/claude-code-internals.md
# §9) — that is why the dispatch-quote concern ALSO carries a reachable
# TaskCreated counterpart. Do not conflate the two.


GATE_REGISTRY: Final[tuple[GateRow, ...]] = (
    GateRow(
        gate_id="enforce-skill-loading",
        handler=router.handle_enforce_skill_loading,
        event="PreToolUse",
        matched="Bash",
        deny_input=lambda _c: _bash("uv run pytest -q"),
        allow_input=lambda _c: {
            "session_id": "sess-liveness",
            "tool_name": "Bash",
            "tool_input": {"command": "uv run pytest -q  # [skill-load-ok: verified-loaded]"},
        },
        arrange=_arrange_skill_loading,
    ),
    GateRow(
        gate_id="enforce-skill-loading-on-task-create",
        handler=router.handle_enforce_skill_loading_on_task_create,
        event="TaskCreated",
        matched="Task",
        deny_input=lambda _c: _task_created("review the acme MR", skip=False),
        allow_input=lambda _c: _task_created("review the acme MR", skip=True),
        arrange=_arrange_skill_loading_on_task,
    ),
    GateRow(
        gate_id="block-edit-before-planned",
        handler=router.handle_block_edit_before_planned,
        event="PreToolUse",
        matched="Edit",
        deny_input=_block_edit_before_planned_deny,
        allow_input=_block_edit_before_planned_allow,
        arrange=_arrange_block_edit_before_planned,
    ),
    GateRow(
        gate_id="block-config-overwrite",
        handler=router.handle_block_config_overwrite,
        event="PreToolUse",
        matched="Write",
        deny_input=_block_config_overwrite_deny,
        allow_input=_block_config_overwrite_allow,
    ),
    GateRow(
        gate_id="protect-default-branch",
        handler=router.handle_protect_default_branch,
        event="PreToolUse",
        matched="Edit",
        deny_input=_protect_branch_deny,
        allow_input=_protect_branch_allow,
    ),
    GateRow(
        gate_id="block-main-clone-mutation",
        handler=router.handle_block_main_clone_mutation,
        event="PreToolUse",
        matched="Bash",
        deny_input=_main_clone_bash_deny,
        allow_input=_main_clone_bash_allow,
    ),
    GateRow(
        gate_id="validate-mr-metadata-bash",
        handler=router.handle_validate_mr_metadata,
        event="PreToolUse",
        matched="Bash",
        deny_input=_mr_meta_bash_deny,
        allow_input=_mr_meta_bash_allow,
    ),
    GateRow(
        gate_id="validate-mr-metadata-mcp",
        handler=router.handle_validate_mr_metadata,
        event="PreToolUse",
        matched="mcp__glab__glab_mr_create",
        deny_input=_mr_meta_mcp_deny,
        allow_input=_mr_meta_mcp_allow,
    ),
    GateRow(
        gate_id="block-self-reviewer-assign-bash",
        handler=router.handle_block_self_reviewer_assign,
        event="PreToolUse",
        matched="Bash",
        deny_input=_reviewer_assign_bash_deny,
        allow_input=_reviewer_assign_bash_allow,
    ),
    GateRow(
        gate_id="block-self-reviewer-assign-mcp",
        handler=router.handle_block_self_reviewer_assign,
        event="PreToolUse",
        matched="mcp__glab__glab_mr_update",
        deny_input=_reviewer_assign_mcp_deny,
        allow_input=_reviewer_assign_mcp_allow,
    ),
    GateRow(
        gate_id="block-ai-signature",
        handler=router.handle_block_ai_signature,
        event="PreToolUse",
        matched="Bash",
        deny_input=_ai_sig_deny,
        allow_input=_ai_sig_allow,
    ),
    GateRow(
        gate_id="quote-scanner-bash",
        handler=router.handle_quote_scanner_pretool,
        event="PreToolUse",
        matched="Bash",
        deny_input=_quote_bash_deny,
        allow_input=_quote_bash_allow,
        arrange=_pin_public_probe,
    ),
    GateRow(
        gate_id="quote-scanner-slack-mcp",
        handler=router.handle_quote_scanner_pretool,
        event="PreToolUse",
        matched="mcp__claude_ai_Slack__slack_send_message",
        deny_input=_quote_slack_deny,
        allow_input=_quote_slack_allow,
        arrange=_arrange_mcp_privacy_gate,
    ),
    GateRow(
        gate_id="block-self-dm-via-mcp",
        handler=router.handle_block_self_dm_via_mcp,
        event="PreToolUse",
        matched="mcp__claude_ai_Slack__slack_send_message",
        deny_input=_self_dm_deny,
        allow_input=_self_dm_allow,
        arrange=_arrange_self_dm_gate,
    ),
    GateRow(
        gate_id="block-mcp-slack-write",
        handler=router.handle_block_mcp_slack_write,
        event="PreToolUse",
        matched="mcp__claude_ai_Slack__slack_send_message",
        deny_input=_mcp_slack_write_deny,
        allow_input=_mcp_slack_write_allow,
    ),
    GateRow(
        gate_id="dispatch-prompt-quote-scanner",
        handler=router.handle_dispatch_prompt_quote_scanner,
        event="PreToolUse",
        matched="Agent",
        deny_input=_dispatch_quote_deny,
        allow_input=_dispatch_quote_allow,
    ),
    GateRow(
        gate_id="dispatch-prompt-quote-scanner-on-task-create",
        handler=router.handle_dispatch_prompt_quote_scanner_on_task_create,
        event="TaskCreated",
        matched="Task",
        deny_input=_dispatch_quote_task_deny,
        allow_input=_dispatch_quote_task_allow,
        arrange=_arrange_dispatch_quote_on_task,
    ),
    GateRow(
        gate_id="banned-terms-bash",
        handler=router.handle_banned_terms_pretool,
        event="PreToolUse",
        matched="Bash",
        deny_input=_banned_bash_deny,
        allow_input=_banned_bash_allow,
        arrange=_arrange_banned_terms,
    ),
    GateRow(
        gate_id="block-uncovered-diff",
        handler=router.handle_block_uncovered_diff,
        event="PreToolUse",
        matched="Bash",
        deny_input=_uncovered_deny,
        allow_input=_uncovered_allow,
    ),
    GateRow(
        gate_id="enforce-orchestrator-boundary-bash",
        handler=router.handle_enforce_orchestrator_boundary,
        event="PreToolUse",
        matched="Bash",
        deny_input=_orch_bash_deny,
        allow_input=_orch_bash_allow,
    ),
    GateRow(
        gate_id="enforce-orchestrator-boundary-agent",
        handler=router.handle_enforce_orchestrator_boundary,
        event="PreToolUse",
        matched="Agent",
        deny_input=_orch_agent_deny,
        allow_input=_orch_agent_allow,
        arrange=_arrange_orch_agent_gate,
    ),
    GateRow(
        gate_id="block-direct-commands",
        handler=router.handle_block_direct_commands,
        event="PreToolUse",
        matched="Bash",
        deny_input=_direct_deny,
        allow_input=_direct_allow,
    ),
    GateRow(
        gate_id="block-out-of-band-merge",
        handler=router.handle_block_out_of_band_merge,
        event="PreToolUse",
        matched="Bash",
        deny_input=_oob_merge_deny,
        allow_input=_oob_merge_allow,
    ),
    GateRow(
        gate_id="block-unknown-repo-push",
        handler=router.handle_block_unknown_repo_push,
        event="PreToolUse",
        matched="Bash",
        deny_input=_unknown_push_deny,
        allow_input=_unknown_push_allow,
    ),
    GateRow(
        gate_id="block-raw-review-post",
        handler=router.handle_block_raw_review_post,
        event="PreToolUse",
        matched="Bash",
        deny_input=_raw_review_deny,
        allow_input=_raw_review_allow,
    ),
    GateRow(
        gate_id="block-raw-pid-kill",
        handler=router.handle_block_raw_pid_kill,
        event="PreToolUse",
        matched="Bash",
        deny_input=_raw_pid_kill_deny,
        allow_input=_raw_pid_kill_allow,
    ),
    GateRow(
        gate_id="block-secret-file-print",
        handler=router.handle_block_secret_file_print,
        event="PreToolUse",
        matched="Bash",
        deny_input=_secret_print_deny,
        allow_input=_secret_print_allow,
    ),
    GateRow(
        gate_id="classifier-deny-stop-gate",
        handler=router.handle_classifier_deny_stop_gate,
        event="Stop",
        matched="Stop",
        deny_input=_classifier_stop_deny,
        allow_input=_classifier_stop_allow,
    ),
)


# No reachability phantoms remain (#1646 / #1733). PR B (#171) repaired the
# ``validate-mr-metadata-mcp`` phantom via the ``mcp__glab__glab_mr_.*`` matcher;
# #1646 then wired the ``Agent`` PreToolUse matcher, making the two PreToolUse
# ``Agent`` arms — ``dispatch-prompt-quote-scanner`` and
# ``enforce-orchestrator-boundary-agent`` — genuinely live. The
# orchestrator-boundary Agent deny is additionally default-ON (#1733) with its
# never-lockout off-ramps intact. The roster is asserted EMPTY explicitly so a
# row silently gaining (or losing) phantom status without a deliberate update is
# caught — the corpus keeps telling the truth either way.
_EXPECTED_REACHABILITY_PHANTOMS: Final[frozenset[str]] = frozenset()
_EXPECTED_ALLOW_PHANTOMS: Final[frozenset[str]] = frozenset()
_EXPECTED_PHANTOM_CATEGORY_COUNT: Final[int] = 0


# ── fixtures (state isolation — the dev's real ~/.teatree.toml can't leak) ──


@pytest.fixture(autouse=True)
def gate_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GateContext]:
    """Pin STATE_DIR and ``Path.home()`` to tmp dirs for every row.

    Mirrors ``test_hook_router_gate_bypass_class.py``: patch ``router.STATE_DIR``
    and ``Path.home`` so neither real session state nor the developer's real
    ``~/.teatree.toml`` can influence (or be influenced by) a gate under test.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    original = router.STATE_DIR
    router.STATE_DIR = state_dir
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("HOME", str(home))
    yield GateContext(tmp_path=tmp_path, monkeypatch=monkeypatch, home=home, state_dir=state_dir)
    router.STATE_DIR = original


def _mark_xfail(request: pytest.FixtureRequest, reason: str | None) -> None:
    if reason is not None:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))


_IDS: Final[list[str]] = [row.gate_id for row in GATE_REGISTRY]


# ── the three mechanical assertions ─────────────────────────────────────────


@pytest.mark.parametrize("row", GATE_REGISTRY, ids=_IDS)
def test_gate_denies_real_must_deny_payload(row: GateRow, gate_ctx: GateContext) -> None:
    """(a) The gate DENIES its real must-DENY payload.

    Holds for every row INCLUDING the phantoms: a phantom's handler logic is
    correct (it denies its real bad input when invoked directly) — what makes
    it a phantom is reachability (c), not its decision logic. So (a) is never
    xfailed.
    """
    row.arrange(gate_ctx)
    event_input = row.deny_input(gate_ctx)
    assert _denied(row.handler, event_input), (
        f"UNDER-FIRE — gate '{row.gate_id}' did not deny its real must-DENY payload.\n  input: {event_input!r}"
    )


@pytest.mark.parametrize("row", GATE_REGISTRY, ids=_IDS)
def test_gate_allows_real_must_allow_payload(
    row: GateRow, request: pytest.FixtureRequest, gate_ctx: GateContext
) -> None:
    """(b) The gate ALLOWS its real must-ALLOW payload.

    No row is xfailed here anymore: the plan-tracker mismatch (#167) is fixed,
    so loading a real skill clears both plan gates and every gate's
    must-ALLOW payload passes through.
    """
    _mark_xfail(request, row.allow_phantom_reason)
    row.arrange(gate_ctx)
    event_input = row.allow_input(gate_ctx)
    assert not _denied(row.handler, event_input), (
        f"OVER-FIRE — gate '{row.gate_id}' denied its real must-ALLOW payload.\n  input: {event_input!r}"
    )


@pytest.mark.parametrize("row", GATE_REGISTRY, ids=_IDS)
def test_gate_is_reachable_on_dispatch_path(row: GateRow, request: pytest.FixtureRequest) -> None:
    """(c) REACHABILITY — the gate's matched tool/skill is actually delivered.

    The phantom detector. A handler keyed on a tool/skill that no registered
    hooks.json matcher delivers to its event fails here.
    """
    _mark_xfail(request, row.phantom_reason)
    assert _gate_is_reachable(row), (
        f"PHANTOM — gate '{row.gate_id}' keys on '{row.matched}' for event "
        f"'{row.event}', which no registered hooks.json matcher delivers. The "
        f"handler logic may be correct but it never fires in production."
    )


# ── loud phantom roster (no silent truncation) ──────────────────────────────


def test_phantom_roster_is_explicit_and_loud() -> None:
    """The known-phantom rows must match the declared rosters.

    Makes the dead-gate roster LOUD: a reader running ``pytest -rsx`` sees each
    xfail reason, and this test fails if a phantom is silently added/removed
    from the registry without updating the expected rosters. After #1646 wired
    the ``Agent`` PreToolUse matcher the roster is EMPTY — the two PreToolUse
    Agent arms are now reachable, and the one CAUSE-B phantom
    (``validate-mr-metadata-mcp``) was repaired earlier by a real matcher.
    """
    reachability = frozenset(row.gate_id for row in GATE_REGISTRY if row.phantom_reason is not None)
    allow = frozenset(row.gate_id for row in GATE_REGISTRY if row.allow_phantom_reason is not None)
    assert reachability == _EXPECTED_REACHABILITY_PHANTOMS, (
        "Reachability-phantom roster drift — update _EXPECTED_REACHABILITY_PHANTOMS.\n"
        f"  rows-marked : {sorted(reachability)}\n  expected    : {sorted(_EXPECTED_REACHABILITY_PHANTOMS)}"
    )
    assert allow == _EXPECTED_ALLOW_PHANTOMS, (
        "Allow-phantom roster drift — update _EXPECTED_ALLOW_PHANTOMS.\n"
        f"  rows-marked : {sorted(allow)}\n  expected    : {sorted(_EXPECTED_ALLOW_PHANTOMS)}"
    )
    distinct_phantom_gates = {gate_id.rsplit("-slack-mcp", 1)[0].rsplit("-mcp", 1)[0] for gate_id in reachability}
    distinct_phantom_gates.update(allow)
    assert len(distinct_phantom_gates) >= _EXPECTED_PHANTOM_CATEGORY_COUNT, (
        f"expected at least the {_EXPECTED_PHANTOM_CATEGORY_COUNT} documented phantom categories, "
        f"got {sorted(distinct_phantom_gates)}"
    )


_NON_DENY_PRETOOLUSE_HANDLERS: Final[frozenset[Callable[[dict], bool | Verdict | None]]] = frozenset(
    {
        # Emits ``permissionDecision=allow`` (or ``None``) — it unblocks the
        # settings.json write, it never denies content.
        router.handle_allow_classifier_relax_settings_write,
        # Side-effect-only mirror — always returns ``False`` (posts the
        # AskUserQuestion to Slack, never denies).
        router.handle_mirror_question_to_slack,
        # Availability router — its deny is a routing conversion of an
        # AskUserQuestion into a DeferredQuestion, not a content/enforcement
        # gate with a must-deny corpus payload.
        router.handle_route_away_mode_question,
        # Responsiveness nudge — advisory only (prints additionalContext once a
        # turn crosses the tool-call budget), returns ``None``, never denies.
        router.handle_orchestrator_turn_budget_nudge,
        # One-decision-per-call advisory — warn-only (stderr nudge on a batched
        # AskUserQuestion), returns ``None``, never denies.
        router.handle_warn_batched_questions,
        # Orchestrator-investigation boundary (#1442) — a WARN-only nudge (stderr
        # + always returns ``False``); it has no deny path, so no must-deny
        # corpus payload.
        router.handle_enforce_orchestrator_investigation_boundary,
    }
)


def test_every_pretooluse_deny_handler_has_a_registry_row() -> None:
    """Coverage guard: every PreToolUse deny gate has a registry row.

    The deny-handler universe is derived from the live registry
    (``router._HANDLERS['PreToolUse']``) minus an explicit, documented
    allow-list of the handlers that legitimately have no :class:`GateRow`
    (``_NON_DENY_PRETOOLUSE_HANDLERS``: allow-emitters, side-effect mirrors,
    routers, bootstrap enforcers). A future deny gate added to the registry
    that is in neither the registry rows NOR the allow-list trips this guard —
    forcing it into the liveness registry rather than slipping in unfired.
    Exempting a genuinely-non-deny handler requires a deliberate, reviewable
    addition to the allow-list, not a silent omission.
    """
    registry: list[Callable[[dict], bool | Verdict | None]] = router._HANDLERS["PreToolUse"]
    allowlisted = _NON_DENY_PRETOOLUSE_HANDLERS - set(registry)
    assert not allowlisted, (
        "Non-deny allow-list names handlers absent from the live PreToolUse "
        f"registry (stale exemptions): {sorted(h.__name__ for h in allowlisted)}"
    )
    deny_handlers = set(registry) - _NON_DENY_PRETOOLUSE_HANDLERS
    covered = {row.handler for row in GATE_REGISTRY}
    missing = deny_handlers - covered
    assert not missing, (
        "PreToolUse deny handlers missing a registry row "
        "(add a GateRow, or add to _NON_DENY_PRETOOLUSE_HANDLERS if genuinely "
        f"non-deny): {sorted(h.__name__ for h in missing)}"
    )
