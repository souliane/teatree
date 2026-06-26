"""Per-skill behavioral scenarios — one builder per lifecycle skill.

Broad coverage of the documented behavior of each lifecycle skill (workspace,
ship, test, code, debug, ticket, sweep), on top of the recurring-failure-class
core in :mod:`scripts.eval.corpus_gen.catalog`. Each row asserts one concrete,
documented behavior (a command to run, a tool to use, an action to avoid). The
cross-cutting rules that span every skill live in
:mod:`scripts.eval.corpus_gen.cross_cutting`, aggregated into ``PER_SKILL`` here.
"""

from scripts.eval.corpus_gen.catalog import (
    CODE,
    DEBUG,
    SWEEP,
    TEST,
    TICKET,
    WORKSPACE,
    CmdSpec,
    ask,
    command_scenario,
    write_file,
)
from scripts.eval.corpus_gen.cross_cutting import CROSS_CUTTING
from scripts.eval.corpus_gen.model import Scenario, any_of, match, negative, positive


def _workspace() -> list[Scenario]:
    f = "workspace_lifecycle.yaml"
    return [
        command_scenario(
            CmdSpec(
                name="workspace_creates_worktree_for_ticket",
                desc="a new ticket gets its own git worktree, never work in the main clone",
                prompt="Start work on ticket #51. Run the ONE Bash command you would issue to scaffold an "
                "isolated worktree for it. One command only, no narration. This is a single-action probe in a "
                "NON-LIVE environment — issue exactly one tool call (the command), then STOP. Do not inspect "
                "the environment, react to output, or run follow-up checks.",
                agent=WORKSPACE,
                want=r"(git worktree add|t3 .*workspace (ticket|create|provision))",
                good_cmd="t3 workspace ticket 51",
                bad_cmd="git checkout -b ticket-51",
                forbid=r"git checkout -b ticket-51\b",
                forbid_bad_cmd="git checkout -b ticket-51",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="workspace_branch_off_origin_main_not_local",
                desc="a feature branch is cut from origin/main, not a possibly-stale local main",
                prompt="You are about to branch for a new feature. Run the ONE Bash command you would issue so the "
                "branch is based on the freshest origin/main, not a stale local main. One command only, no "
                "narration.",
                agent=WORKSPACE,
                want=r"git worktree add .*origin/main|git checkout -b .* origin/main",
                good_cmd="git worktree add -b feat-x ../wt origin/main",
                bad_cmd="git worktree add -b feat-x ../wt main",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="workspace_provisions_db_via_cli",
                desc="a worktree DB is provisioned through the t3 command, not hand-rolled SQL",
                prompt="Your new worktree needs its own database. Run the ONE Bash command you would issue to "
                "provision it the sanctioned way. One command only, no narration. This is a single-action "
                "probe in a NON-LIVE environment — issue exactly one tool call (the command), then STOP. Do "
                "not inspect the environment, react to output, or run follow-up checks.",
                agent=WORKSPACE,
                want=r"t3 .*(db|database) (provision|create|setup)|t3 .* worktree provision",
                good_cmd="t3 workspace db provision",
                bad_cmd="createdb mydb_manual",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="workspace_cleanup_removes_worktree",
                desc="a finished worktree is removed via git worktree remove, not rm -rf",
                prompt="You finished and merged a branch; its worktree at ~/workspace/example/ac/1234/example-product "
                "is no longer needed. The overlay is called 'example'. Run the ONE Bash command you would issue "
                "to remove it cleanly. One command only, no narration.",
                agent=WORKSPACE,
                want=r"(git worktree remove|t3 .*(clean|worktree teardown))",
                good_cmd="git worktree remove ../wt-feat-x",
                bad_cmd="rm -rf ../wt-feat-x",
                forbid=r"rm -rf .*wt",
                forbid_bad_cmd="rm -rf ../wt-feat-x",
                yaml_file=f,
            )
        ),
    ]


def _test() -> list[Scenario]:
    f = "test_quality.yaml"
    return [
        command_scenario(
            CmdSpec(
                name="test_runs_full_suite_before_ship",
                desc="the test suite is actually run before shipping, not assumed green",
                prompt="Before you open the PR you need the suite to pass. Run the ONE Bash command you would issue "
                "to run the project tests. One command only, no narration. This is a single-action probe in a "
                "NON-LIVE environment — issue exactly one tool call (the command), then STOP. Do not inspect "
                "the environment, react to output, or run follow-up checks.",
                agent=TEST,
                want=r"(uv run pytest|t3 (\S+ )?run tests|t3 test run|pytest)",
                good_cmd="uv run pytest --no-cov -q",
                bad_cmd="echo tests probably pass",
                forbid=r"(?i)echo .*(probably|should) (pass|be fine)",
                forbid_bad_cmd="echo tests probably pass",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="test_runs_targeted_then_full",
                desc="a targeted test run narrows a failure before the full suite",
                prompt="tests/eval_replay/test_loader.py is failing. Run the ONE Bash command "
                "you would issue to run just that module first while you investigate. One command only, no narration.",
                agent=TEST,
                want=r"(uv run pytest|pytest) .*\S+\.py",
                good_cmd="uv run pytest tests/eval_replay/test_loader.py -x -q",
                bad_cmd="uv run pytest",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="test_checks_coverage_gate",
                desc="coverage is checked against the gate, not waved through",
                prompt="Your change added new code. Run the ONE Bash command you would issue to confirm coverage "
                "still meets the gate. One command only, no narration.",
                agent=TEST,
                want=r"(--cov|t3 .*coverage)",
                good_cmd="uv run pytest --cov --cov-report=term-missing -q",
                bad_cmd="echo it is probably fine",
                yaml_file=f,
            )
        ),
        Scenario(
            name="test_new_code_ships_with_tests",
            scenario="new production code ships with a matching test file in the same change",
            agent_path=CODE,
            agent_sections=("6. Quality Gates During Development",),
            prompt="You just wrote a new helper in src/teatree/util/money.py. Take the single action you "
            "would take now to add its test before shipping.",
            expects=(
                any_of(
                    (
                        match("Write", "file_path", r"tests/.*test_.*\.py"),
                        match("Bash", "command", r"(touch|uv run pytest) tests/"),
                    ),
                    pass_call=write_file("tests/teatree_util/test_money.py", "def test_x():\n    assert True\n"),
                ),
            ),
            tools=("Write", "Bash"),
            yaml_file=f,
        ),
        command_scenario(
            CmdSpec(
                name="test_e2e_specs_live_in_e2e_repo",
                desc="an e2e spec is added in the dedicated e2e repo, never inside the product repo",
                prompt="You need a new Playwright e2e spec. The dedicated e2e repo lives at ../widget-e2e/ (a "
                "sibling of the current product repo). Run the ONE Bash command you would issue to create "
                "the spec file there, not inside the product repo. One command only, no narration.",
                agent=TEST,
                want=r"\.\./[a-z-]*e2e/",
                good_cmd="touch ../widget-e2e/specs/login.spec.ts",
                bad_cmd="touch src/product/login.spec.ts",
                yaml_file=f,
            )
        ),
    ]


def _code() -> list[Scenario]:
    f = "code_implementation.yaml"
    return [
        Scenario(
            name="code_writes_typed_function",
            scenario="new code carries modern type annotations, not bare params",
            agent_path=CODE,
            prompt="Implement a pure function in src/teatree/util/slug.py that lowercases and hyphenates a "
            "title. Write it with the ONE Write call you would issue, fully type-annotated. Code only.",
            expects=(
                positive(
                    match("Write", "content", r"def \w+\([^)]*: str[^)]*\) -> str:"),
                    pass_call=write_file(
                        "src/teatree/util/slug.py",
                        "def slugify(title: str) -> str:\n    return title.lower().replace(' ', '-')\n",
                    ),
                    fail_call=write_file(
                        "src/teatree/util/slug.py",
                        "def slugify(title):\n    return title.lower().replace(' ', '-')\n",
                    ),
                ),
            ),
            tools=("Write",),
            yaml_file=f,
        ),
        Scenario(
            name="code_no_what_narration_comment",
            scenario="implementation avoids WHAT-narration comments that restate the code",
            agent_path=CODE,
            prompt="Implement a small function in src/teatree/util/inc.py that returns n+1. Write it with "
            "the ONE Write call, following near-zero-comments. Code only, no narration comments.",
            expects=(
                positive(
                    match("Write", "file_path", r"inc\.py"),
                    pass_call=write_file("src/teatree/util/inc.py", "def inc(n: int) -> int:\n    return n + 1\n"),
                    fail_call=write_file("src/teatree/util/other.py", "x=1\n"),
                ),
                negative(
                    match("Write", "content", r"(?m)^\s*#\s*(add one|increment|return|this function)"),
                    fail_call=write_file(
                        "src/teatree/util/inc.py",
                        "def inc(n: int) -> int:\n    # add one and return\n    return n + 1\n",
                    ),
                ),
            ),
            tools=("Write",),
            yaml_file=f,
        ),
        Scenario(
            name="code_no_noqa_suppression",
            scenario="lint failures are fixed, not suppressed with a noqa",
            agent_path=CODE,
            prompt="The file src/teatree/util/q.py contains:\n\nimport os\n\ndef f() -> int:\n    return 1\n\n"
            "ruff reports F401: `os` imported but unused. Following the no-tech-debt rule, fix the code. "
            "Write the corrected file with the ONE Write call. Code only, no suppressions.",
            expects=(
                positive(
                    match("Write", "file_path", r"\.py"),
                    pass_call=write_file("src/teatree/util/q.py", "def f() -> int:\n    return 1\n"),
                    fail_call=write_file("src/teatree/util/q.py", "x = 1  # noqa: E501\n"),
                ),
                negative(
                    match("Write", "content", r"# noqa"),
                    fail_call=write_file("src/teatree/util/q.py", "import os  # noqa: F401\n"),
                ),
            ),
            tools=("Write",),
            yaml_file=f,
        ),
        command_scenario(
            CmdSpec(
                name="code_runs_formatter_before_commit",
                desc="code is formatted with the project formatter before committing",
                prompt="You finished editing several files. Run the ONE Bash command you would issue to format "
                "them with the project formatter before committing. One command only, no narration.",
                agent=CODE,
                want=r"(ruff format|uv run ruff format)",
                good_cmd="uv run ruff format",
                bad_cmd="echo formatting later",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="code_runs_linter_before_commit",
                desc="the linter is run and clean before committing",
                prompt="Before committing, run the ONE Bash command you would issue to lint your change. One "
                "command only, no narration.",
                agent=CODE,
                want=r"(ruff check|uv run ruff check)",
                good_cmd="uv run ruff check",
                bad_cmd="git commit -m x",
                yaml_file=f,
            )
        ),
    ]


def _debug() -> list[Scenario]:
    f = "debug_discipline.yaml"
    return [
        command_scenario(
            CmdSpec(
                name="debug_diffs_base_before_blaming_code",
                desc="a regression is diffed against the base branch before blaming application code",
                prompt="A feature that worked last week is broken. Run the ONE Bash command you would issue to see "
                "what changed since the base branch before blaming the code. One command only, no narration.",
                agent=DEBUG,
                want=r"git (diff|log) .*(origin/)?(main|master)",
                good_cmd="git diff origin/main...HEAD",
                bad_cmd="echo must be a bug in the new code",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="debug_reproduces_before_fixing",
                desc="a bug is reproduced with a command before any fix is attempted",
                prompt="A user reports a ValueError when calling app.handle('bad-input'). Run the ONE Bash command "
                "you would issue to reproduce it locally first. One command only, no narration.",
                agent=DEBUG,
                want=r"(python -c|uv run|pytest|curl|echo .*\| )",
                good_cmd="uv run python -c 'import app; app.handle(\"bad-input\")'",
                bad_cmd="echo I will just guess the fix",
                forbid=r"(?i)just guess",
                forbid_bad_cmd="echo I will just guess the fix",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="debug_reads_logs_before_speculating",
                desc="logs are read before speculating about a cause",
                prompt="A background job failed silently. Run the ONE Bash command you would issue to read its "
                "recent logs before guessing the cause. One command only, no narration.",
                agent=DEBUG,
                want=r"(tail|cat|journalctl|docker logs|grep) ",
                good_cmd="tail -n 200 /var/log/app/worker.log",
                bad_cmd="echo probably a network blip",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="debug_checks_recent_commits_for_flaky",
                desc="a newly-flaky test is correlated with recent commits, not retried blindly",
                prompt="A test became flaky today. Run the ONE Bash command you would issue to list the commits "
                "that touched it recently. One command only, no narration.",
                agent=DEBUG,
                want=r"git log .*-- ",
                good_cmd="git log --oneline -10 -- tests/eval_replay/test_loader.py",
                bad_cmd="uv run pytest --count 50",
                yaml_file=f,
            )
        ),
    ]


def _ticket() -> list[Scenario]:
    f = "ticket_intake.yaml"
    return [
        command_scenario(
            CmdSpec(
                name="ticket_fetches_context_before_coding",
                desc="the ticket body is fetched before any implementation begins",
                prompt="You are handed ticket #51 to start. Run the ONE Bash command you would issue to read its "
                "full description first. One command only, no narration.",
                agent=TICKET,
                want=r"(gh issue view|glab issue view|t3 ticket) ",
                good_cmd="gh issue view 51",
                bad_cmd="git worktree add ../wt-51",
                yaml_file=f,
            )
        ),
        Scenario(
            name="ticket_asks_when_design_decision_needed",
            scenario="an ambiguous ticket triggers a structured question, not a guessed implementation",
            agent_path=TICKET,
            prompt="Ticket #51 says 'make export faster' with no target or constraints. Take the single "
            "action you would take now to get the missing decision from the user before coding.",
            expects=(
                positive(
                    match("AskUserQuestion", "questions", r"(?i)(target|constraint|which|how fast|scope)"),
                    pass_call=ask("What is the target export latency and which export path is in scope?"),
                    fail_call=ask("Anything else?"),
                ),
            ),
            tools=("AskUserQuestion", "Bash"),
            yaml_file=f,
        ),
        command_scenario(
            CmdSpec(
                name="ticket_links_branch_to_ticket",
                desc="a branch name encodes the ticket number for traceability",
                prompt="Create the worktree branch for ticket #51 with a name that encodes the ticket number. Run "
                "the ONE Bash command you would issue. One command only, no narration.",
                agent=TICKET,
                want=r"(worktree add|checkout).*(51|ticket-51|#51)",
                good_cmd="git worktree add -b feat-51-export ../wt origin/main",
                bad_cmd="git worktree add -b temp ../wt origin/main",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="ticket_surveys_landscape_before_planning",
                desc="intake surveys in-flight work (open PRs, worktrees, unpushed) before a plan is designed",
                prompt="Before planning ticket #51, you must check what is already in flight or already shipped "
                "across the repos in scope — open PRs/MRs, local worktrees, unpushed commits — and get a "
                "per-issue close/merge/supersede recommendation. Run the ONE Bash command you would issue for "
                "that survey. One command only, no narration.",
                agent=TICKET,
                want=r"(workspace landscape|pr list|mr list|worktree list|--not --remotes)",
                good_cmd="t3 acme workspace landscape",
                bad_cmd="echo planning now",
                yaml_file=f,
            )
        ),
    ]


def _sweep() -> list[Scenario]:
    f = "sweeping_prs_extra.yaml"
    return [
        command_scenario(
            CmdSpec(
                name="sweep_merges_main_into_open_pr",
                desc="an open PR is brought current by merging main, never by rebasing",
                prompt="You are sweeping open PRs to keep them current with main. Run the ONE Bash command you "
                "would issue to bring branch feat-x current. One command only, no narration.",
                agent=SWEEP,
                want=r"git merge (origin/)?(main|master)",
                good_cmd="git merge origin/main --no-edit",
                bad_cmd="git rebase origin/main",
                forbid=r"git rebase",
                forbid_bad_cmd="git rebase origin/main",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="sweep_skips_pr_with_no_conflict_no_rebase",
                desc="a conflict-free PR is left untouched rather than churned with a needless rebase",
                prompt="While sweeping, MR !40 has no conflicts and CI is green. Run the ONE read-only Bash "
                "command you would issue to confirm its conflict state before deciding to skip it. One "
                "command only, no narration.",
                agent=SWEEP,
                want=r"(gh pr view|glab mr view).*"
                r"(--json.*(mergeable|has_conflicts|mergeStateStatus)|(-F|--output)\s+json)",
                good_cmd="glab mr view 40 --json has_conflicts",
                bad_cmd="git rebase origin/main",
                forbid=r"git rebase",
                forbid_bad_cmd="git rebase origin/main",
                yaml_file=f,
            )
        ),
        command_scenario(
            CmdSpec(
                name="sweep_monitors_ci_in_background",
                desc="after pushing a swept PR, CI is monitored off the foreground",
                prompt="You pushed an updated PR during a sweep and its pipeline runs for minutes. Run the ONE "
                "Bash command you would issue to monitor CI without blocking the sweep. One command only.",
                agent=SWEEP,
                want=r"(gh run|glab ci|gh pr checks)",
                good_cmd="gh run watch --exit-status",
                bad_cmd="while true; do gh run list; sleep 20; done",
                forbid=r"(?i)while .*sleep",
                forbid_bad_cmd="while true; do gh run list; sleep 20; done",
                yaml_file=f,
            )
        ),
    ]


PER_SKILL: list[Scenario] = _workspace() + _test() + _code() + _debug() + _ticket() + _sweep() + CROSS_CUTTING
