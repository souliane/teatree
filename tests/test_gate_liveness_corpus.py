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

After PR B (#171) three gates remain phantoms — the PreToolUse ``Agent`` arms of
the plan-gate, dispatch-quote, and orchestrator-boundary gates. Their handler
logic is correct but assertion (c) fails because NO ``Agent`` matcher is wired
in ``hooks.json`` (the registered PreToolUse matchers are ``Bash|Edit|Write``,
``AskUserQuestion``, ``mcp__.*[Ss]lack.*``; ``Agent`` has only ever appeared in
the PostToolUse matcher). The ``Agent`` TOOL itself DOES reach PreToolUse —
adding an ``Agent`` matcher would make these arms genuinely live — so they are
left unwired DELIBERATELY, not because the tool bypasses the event. The deny
that matters most (the orchestrator-boundary foreground-Agent guard) sits on the
orchestrator's own hot path, so enabling it unattended is a lockout risk to be
validated attended (#1646). Distinguish this from the ``Task``/``Workflow``
fan-out vehicle, which genuinely DOES bypass PreToolUse and fires ``TaskCreated``
instead (no ``run_in_background`` in its schema) — that is why the plan-gate and
dispatch-quote concerns also carry reachable ``TaskCreated`` counterparts.
They are ``xfail(strict=True)`` so the suite is GREEN now and any later genuine
fix flips the row to an unexpected-pass that FAILS the build, forcing the xfail
removal. The final assertion makes the phantom roster LOUD (also visible via
``-rsx``) so a reader sees exactly which gates are known-dead — no silent
truncation.

The plan-gate rows use the real ``teatree-plan`` / ``t3:teatree-plan`` skill
name. The tracker now matches it by exact final-segment membership, so a real
``/plan`` clears the gate (the validity hole is closed); the new
``TaskCreated`` plan-gate row proves the fan-out path is enforced too.
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

# ── environment & invocation context ────────────────────────────────────

_HOOKS_JSON: Final[Path] = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
_REPO_SKILLS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "skills"

# The real plan skill (skills/teatree-plan/SKILL.md frontmatter name), invoked
# as ``t3:teatree-plan``. Its final path segment is ``teatree-plan`` — which
# does NOT start with ``plan``, so the plan-gate tracker never records it.
_REAL_PLAN_SKILL: Final[str] = "teatree-plan"
_REAL_PLAN_SKILL_INVOCATION: Final[str] = "t3:teatree-plan"


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
    handler: Callable[[dict], bool | None]
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


def _denied(handler: Callable[[dict], bool | None], event_input: dict) -> bool:
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
    ctx.write_state("pending", f"{_REAL_PLAN_SKILL}\n")
    ctx.write_state("skills", "")


def _arrange_skill_loading_on_task(ctx: GateContext) -> None:
    ctx.monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_REPO_SKILLS_DIR))
    ctx.write_state("pending", f"{_REAL_PLAN_SKILL}\n")
    ctx.write_state("skills", "")


def _task_created(description: str, *, skip: bool) -> dict:
    token = "[skip-skill-gate: false-trigger] " if skip else ""
    return {
        "session_id": "sess-liveness",
        "task_subject": "do the thing",
        "task_description": f"{token}{description}",
    }


# plan gate (PreToolUse Edit/Write): opt-in per overlay; an Edit under the
# workspace with neither a recorded /plan nor a recorded read must block.


def _arrange_plan_gate(ctx: GateContext) -> None:
    ctx.write_teatree_toml("[overlays.acme]\nplan_gate = true\n")
    ws = ctx.home / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    ctx.monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))


def _edit_in_workspace(ctx: GateContext, *, satisfied: bool) -> dict:
    target = ctx.home / "workspace" / "acme" / "module.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    if satisfied:
        # The real plan skill is invoked as t3:teatree-plan; record what the
        # PostToolUse tracker would actually write for it.
        router.handle_track_plan_invocation(
            {
                "session_id": ctx.session_id,
                "tool_name": "Skill",
                "tool_input": {"skill": _REAL_PLAN_SKILL_INVOCATION},
            }
        )
    return {
        "session_id": ctx.session_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
    }


# agent plan gate (Agent/Task): a fresh /plan timestamp or a [skip-plan-gate]
# token clears it. The real /plan is t3:teatree-plan.


def _arrange_agent_plan_gate(ctx: GateContext) -> None:
    ctx.monkeypatch.setenv("XDG_DATA_HOME", str(ctx.tmp_path / "xdg"))
    ctx.monkeypatch.delenv("TEATREE_PLAN_GATE_WINDOW_MINUTES", raising=False)


def _agent_plan_allow(ctx: GateContext) -> dict:
    # Record a /plan exactly as the PostToolUse tracker would for the REAL
    # plan skill name — proving (or disproving) a real /plan clears the gate.
    router.handle_track_plan_skill_timestamp(
        {"tool_name": "Skill", "tool_input": {"skill": _REAL_PLAN_SKILL_INVOCATION}}
    )
    return _agent("implement the acme feature", run_in_background=True)


# plan gate (TaskCreated): the fan-out path. A fanned-out Task with no recent
# /plan must block; a real t3:teatree-plan or a [skip-plan-gate] token clears it.


def _arrange_task_created_plan_gate(ctx: GateContext) -> None:
    # The TaskCreated plan-gate ships default-OFF (opt-in, pending the
    # correct-signal design in #1640), so the corpus must explicitly enable it
    # to prove it CAN fire when on: reachable + denies a missing-plan fan-out +
    # allows a planned one.
    _arrange_agent_plan_gate(ctx)
    ctx.write_teatree_toml("[teatree]\nagent_plan_gate_on_task_create_enabled = true\n")


def _task_created_plan(*, skip: bool) -> dict:
    token = "[skip-plan-gate: false-trigger] " if skip else ""
    return {
        "session_id": "sess-liveness",
        "task_subject": f"{token}build the acme feature",
        "task_description": "",
    }


def _task_created_plan_allow(ctx: GateContext) -> dict:
    router.handle_track_plan_skill_timestamp(
        {"tool_name": "Skill", "tool_input": {"skill": _REAL_PLAN_SKILL_INVOCATION}}
    )
    return _task_created_plan(skip=False)


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


def _quote_bash_deny(ctx: GateContext) -> dict:
    return _bash(f'gh issue create --title t --body "{_HIGH_QUOTE}"')


def _quote_bash_allow(ctx: GateContext) -> dict:
    return _bash('gh issue create --title t --body "Routine status update."')


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


# dispatch-prompt quote-scanner (Agent/Task): a dispatch prompt carrying a
# verbatim user quote denies; a clean prompt allows. Phantom because no Agent
# matcher is wired in hooks.json (the Agent tool itself DOES reach PreToolUse —
# adding the matcher would make this arm live). The clean-prompt fan-out concern
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


def _banned_bash_deny(ctx: GateContext) -> dict:
    return _bash(f'gh issue create --title t --body "{_BANNED_BODY}"')


def _banned_bash_allow(ctx: GateContext) -> dict:
    return _bash('gh issue create --title t --body "Rolling out the integration."')


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
# from the main agent denies — but no Agent matcher is wired in hooks.json, so
# this arm is phantom. The Agent tool DOES reach PreToolUse (run_in_background is
# present in its tool_input), so adding the matcher would make this arm live;
# it is left unwired DELIBERATELY because the deny sits on the orchestrator's own
# foreground-Agent-dispatch hot path — enabling it unattended is a lockout risk
# to be validated attended (#1646). (Unlike the plan-gate/dispatch-quote arms it
# has NO TaskCreated counterpart: the TaskCreated schema has no run_in_background,
# this gate's only signal — so the Agent-matcher path is its only fix.) The deny
# ships default-OFF behind orchestrator_boundary_agent_gate_enabled; the arrange
# enables it so assertion (a) still proves the handler denies its real must-DENY
# payload. run_in_background / a [fg-ok: <reason>] token clears it.


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


# block-raw-review-post (PreToolUse Bash): a raw forge REST WRITE to a review
# endpoint denies; a bare GET read allows.


def _raw_review_deny(_ctx: GateContext) -> dict:
    return _bash("glab api projects/1/merge_requests/1/discussions -X POST -f body=lgtm")


def _raw_review_allow(_ctx: GateContext) -> dict:
    return _bash("glab api projects/1/merge_requests/1/discussions")


# classifier-deny stop gate (Stop): a pending classifier-deny marker emits the
# STOP-and-explain systemMessage; no marker allows the Stop chain to proceed.


def _classifier_stop_deny(ctx: GateContext) -> dict:
    ctx.write_state("classifier-deny", json.dumps({"tool_name": "Bash", "action": "git push"}))
    return {"session_id": ctx.session_id}


def _classifier_stop_allow(ctx: GateContext) -> dict:
    return {"session_id": ctx.session_id}


# ── the registry ──────────────────────────────────────────────────────────

# These three are phantom because NO `Agent` matcher is wired in hooks.json (the
# registered PreToolUse matchers are `Bash|Edit|Write`, `AskUserQuestion`,
# `mcp__.*[Ss]lack.*`; `Agent` has only ever been in the PostToolUse matcher).
# The `Agent` TOOL itself DOES reach PreToolUse, so adding an `Agent` matcher
# would make these arms genuinely live — they are kept unwired DELIBERATELY (the
# deliberate STEP 0 deviation in #171 PR B), not because the tool bypasses the
# event. The plan-gate and dispatch-quote concerns also carry reachable
# TaskCreated counterparts because the SEPARATE `Task`/`Workflow` fan-out vehicle
# genuinely DOES bypass PreToolUse (verified against the Claude Code binary;
# docs/claude-code-internals.md §9). Do not conflate the two.
_AGENT_PLAN_GATE_PHANTOM = (
    "phantom — no `Agent` PreToolUse matcher is wired in hooks.json; the `Agent` tool DOES "
    "reach PreToolUse, so adding one would make this arm live. Kept unwired deliberately; "
    "the fan-out path is enforced by the reachable enforce-plan-gate-on-task-create — see #1646"
)
_DISPATCH_QUOTE_PHANTOM = (
    "phantom — no `Agent` PreToolUse matcher is wired in hooks.json; the `Agent` tool DOES "
    "reach PreToolUse, so adding one would make this arm live. Kept unwired deliberately; the "
    "fan-out path is enforced by the reachable dispatch-prompt-quote-scanner-on-task-create — see #1646"
)
_ORCH_AGENT_PHANTOM = (
    "phantom — no `Agent` PreToolUse matcher is wired in hooks.json; the `Agent` tool DOES reach "
    "PreToolUse (run_in_background present in its tool_input), so adding one would make this arm live. "
    "Kept unwired deliberately: enabling the deny on the orchestrator's own foreground Agent-dispatch "
    "hot path is a lockout risk to be validated attended. It has no TaskCreated counterpart (that "
    "schema has no run_in_background), so the Agent-matcher path is its only fix; ships default-OFF "
    "behind orchestrator_boundary_agent_gate_enabled — see #1646"
)


GATE_REGISTRY: Final[tuple[GateRow, ...]] = (
    GateRow(
        gate_id="enforce-skill-loading",
        handler=router.handle_enforce_skill_loading,
        event="PreToolUse",
        matched="Bash",
        deny_input=lambda _c: _bash("ls -la"),
        allow_input=lambda _c: {
            "session_id": "sess-liveness",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la [skill-load-ok: verified-loaded]"},
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
        gate_id="enforce-plan-gate",
        handler=router.handle_enforce_plan_gate,
        event="PreToolUse",
        matched="Edit",
        deny_input=lambda c: _edit_in_workspace(c, satisfied=False),
        allow_input=lambda c: _edit_in_workspace(c, satisfied=True),
        arrange=_arrange_plan_gate,
    ),
    GateRow(
        gate_id="enforce-agent-plan-gate",
        handler=router.handle_enforce_agent_plan_gate,
        event="PreToolUse",
        matched="Agent",
        deny_input=lambda _c: _agent("implement the acme feature", run_in_background=True),
        allow_input=_agent_plan_allow,
        arrange=_arrange_agent_plan_gate,
        phantom_reason=_AGENT_PLAN_GATE_PHANTOM,
    ),
    GateRow(
        gate_id="enforce-plan-gate-on-task-create",
        handler=router.handle_enforce_plan_gate_on_task_create,
        event="TaskCreated",
        matched="Task",
        deny_input=lambda _c: _task_created_plan(skip=False),
        allow_input=_task_created_plan_allow,
        arrange=_arrange_task_created_plan_gate,
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
        gate_id="dispatch-prompt-quote-scanner",
        handler=router.handle_dispatch_prompt_quote_scanner,
        event="PreToolUse",
        matched="Agent",
        deny_input=_dispatch_quote_deny,
        allow_input=_dispatch_quote_allow,
        phantom_reason=_DISPATCH_QUOTE_PHANTOM,
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
        phantom_reason=_ORCH_AGENT_PHANTOM,
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
        gate_id="block-raw-review-post",
        handler=router.handle_block_raw_review_post,
        event="PreToolUse",
        matched="Bash",
        deny_input=_raw_review_deny,
        allow_input=_raw_review_allow,
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


# The remaining known phantom CATEGORIES after PR B (#171). PR B repaired the
# one genuine CAUSE-B phantom — ``validate-mr-metadata-mcp`` — by adding the
# ``mcp__glab__glab_mr_.*`` PreToolUse matcher (an ordinary MCP call merely
# omitted from the matcher; it now fires in production). What stays phantom is
# the THREE PreToolUse Agent arms — phantom because NO ``Agent`` matcher is wired
# in ``hooks.json``, NOT because the Agent tool bypasses the event. The ``Agent``
# TOOL itself DOES reach PreToolUse (a foreground Agent dispatch fires it with
# ``run_in_background`` in the tool_input), so adding an ``Agent`` matcher would
# make all three genuinely live. They are kept unwired DELIBERATELY in this PR:
# the orchestrator-boundary deny in particular sits on the orchestrator's own
# foreground Agent-dispatch hot path, so enabling it must be validated attended
# (#1646). Two of them ALSO carry reachable TaskCreated counterparts
# (``enforce-plan-gate-on-task-create``, ``dispatch-prompt-quote-scanner-on-task-create``)
# because the SEPARATE ``Task``/``Workflow`` fan-out vehicle genuinely DOES bypass
# PreToolUse (verified against the Claude Code binary; docs/claude-code-internals.md
# §9); the orchestrator-boundary arm has no such counterpart because the TaskCreated
# schema lacks ``run_in_background``. They stay xfail (NOT given an ``Agent``
# matcher) so the corpus keeps telling the truth. The categories are asserted
# explicitly below so a row losing/gaining its phantom status without a deliberate
# update is caught.
_EXPECTED_REACHABILITY_PHANTOMS: Final[frozenset[str]] = frozenset(
    {
        "enforce-agent-plan-gate",  # no Agent matcher wired; reachable via TaskCreated counterpart too
        "dispatch-prompt-quote-scanner",  # no Agent matcher wired; reachable via TaskCreated counterpart too
        "enforce-orchestrator-boundary-agent",  # no Agent matcher wired; no TaskCreated signal, deferred (#1646)
    }
)
# No allow-phantoms remain: the plan-tracker mismatch (#167) is fixed, so a
# real teatree-plan /plan now clears both plan gates.
_EXPECTED_ALLOW_PHANTOMS: Final[frozenset[str]] = frozenset()
_EXPECTED_PHANTOM_CATEGORY_COUNT: Final[int] = 3


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
    so a real ``teatree-plan`` /plan clears both plan gates and every gate's
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
    from the registry without updating the expected rosters. After PR B (#171)
    the roster is exactly the three PreToolUse Agent arms, phantom because no
    ``Agent`` matcher is wired in ``hooks.json`` (the Agent tool DOES reach
    PreToolUse — they are kept unwired deliberately, see #1646), while the one
    genuine CAUSE-B phantom, ``validate-mr-metadata-mcp``, was repaired by a
    real matcher.
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


_NON_DENY_PRETOOLUSE_HANDLERS: Final[frozenset[Callable[[dict], bool | None]]] = frozenset(
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
        # Loop-bootstrap enforcer — its deny is a one-off setup nudge to
        # register the background-loop cron, not a content/enforcement gate.
        router.handle_enforce_loop_registration,
        # Responsiveness nudge — advisory only (prints additionalContext once a
        # turn crosses the tool-call budget), returns ``None``, never denies.
        router.handle_orchestrator_turn_budget_nudge,
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
    registry: list[Callable[[dict], bool | None]] = router._HANDLERS["PreToolUse"]
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
