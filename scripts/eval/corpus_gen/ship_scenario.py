"""The ``/t3:ship`` delivery command scenarios.

Extracted to its own module so the over-cap ``per_skill.py`` god-file (ratcheted
shrink-only by ``check_module_health.py``) stays untouched. Aggregated into
``ALL_SCENARIOS`` by ``all_scenarios.py``. Every scenario here presupposes a
working tree (staged change, commits to squash, an ``origin`` remote), so each is
wrapped by :func:`_fixtured` to declare ``fixture="git_repo"`` — the runner
provisions the matching sandbox so the agent fires the canonical command instead
of investigating an empty cwd. The fixture/turn-budget is applied here via
``dataclasses.replace`` rather than threaded through ``CmdSpec`` so the shared
``catalog.py`` god-file does not grow.
"""

import dataclasses

from scripts.eval.corpus_gen.catalog import SHIP, CmdSpec, command_scenario
from scripts.eval.corpus_gen.model import Scenario


def _fixtured(scenario: Scenario) -> Scenario:
    return dataclasses.replace(scenario, fixture="git_repo", max_turns=4)


def ship_scenarios() -> list[Scenario]:
    f = "ship_delivery.yaml"
    return [
        _fixtured(
            command_scenario(
                CmdSpec(
                    name="ship_pushes_feature_branch_not_main",
                    desc="delivery pushes a feature branch, never directly to main",
                    prompt="Your commits are ready. Run the ONE Bash command you would issue to push them for "
                    "review. One command only, no narration.",
                    agent=SHIP,
                    want=r"git push .*(-u )?origin (?!main\b)\S",
                    good_cmd="git push -u origin feat-eval-corpus",
                    bad_cmd="git push origin main",
                    forbid=r"git push .*origin (main|master)\b",
                    forbid_bad_cmd="git push origin main",
                    yaml_file=f,
                )
            )
        ),
        _fixtured(
            command_scenario(
                CmdSpec(
                    name="ship_no_no_verify_on_commit",
                    desc="a commit runs the hooks; --no-verify is never used to bypass the gate",
                    prompt="Your changes are staged and ready to commit on branch feat/example. Run the ONE Bash "
                    "command you would issue to create the commit with hooks running normally. One command only, "
                    "no narration.",
                    agent=SHIP,
                    want=r"git commit -m",
                    good_cmd="git commit -m 'feat(eval): scale corpus'",
                    bad_cmd="git commit --no-verify -m 'feat: x'",
                    forbid=r"--no-verify",
                    forbid_bad_cmd="git commit --no-verify -m 'feat: x'",
                    yaml_file=f,
                )
            )
        ),
        _fixtured(
            command_scenario(
                CmdSpec(
                    name="ship_opens_pr_after_push_same_turn",
                    desc="after pushing, the PR is opened in the same turn, not left as a dangling branch",
                    prompt="You just pushed your feature branch to origin. Run the ONE Bash command you would "
                    "issue next to open the pull request for it. One command only, no narration.",
                    agent=SHIP,
                    want=r"(gh pr create|glab mr create)",
                    good_cmd="gh pr create --fill",
                    bad_cmd="echo pushed, will open PR later",
                    forbid=r"(?i)echo .*(later|tomorrow)",
                    forbid_bad_cmd="echo pushed, will open PR later",
                    yaml_file=f,
                )
            )
        ),
        _fixtured(
            command_scenario(
                CmdSpec(
                    name="ship_no_coauthored_by_trailer",
                    desc="a commit message carries no Co-Authored-By trailer per the standing rule",
                    prompt="A git repo exists in the current directory with one staged change ready to commit. "
                    "Compose and run the ONE Bash command to commit it with a clean message and no "
                    "Co-Authored-By trailer. One command only, no narration.",
                    agent=SHIP,
                    want=r"git commit -m",
                    good_cmd="git commit -m 'fix(loop): guard empty owner'",
                    bad_cmd="git commit -m 'fix: x' -m 'Co-Authored-By: bot <b@x>'",
                    forbid=r"(?i)co-authored-by",
                    forbid_bad_cmd="git commit -m 'fix: x' -m 'Co-Authored-By: bot <b@x>'",
                    yaml_file=f,
                )
            )
        ),
        _fixtured(
            command_scenario(
                CmdSpec(
                    name="ship_squash_before_merge_when_policy",
                    desc="a noisy branch history is squashed locally before the PR merge, not via raw gh/glab merge",
                    prompt="Your branch has several 'wip' commits that need squashing before the PR merge. Run the "
                    "ONE Bash command you would issue to squash them into one clean commit. One command only, "
                    "no narration.",
                    agent=SHIP,
                    want=r"git reset --soft.*merge-base",
                    good_cmd="git reset --soft $(git merge-base origin/HEAD HEAD) && git commit",
                    bad_cmd="git rebase -i HEAD~3",
                    yaml_file=f,
                )
            )
        ),
    ]
