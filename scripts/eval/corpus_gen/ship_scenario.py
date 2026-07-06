"""The ``/t3:ship`` delivery command scenarios.

Extracted to its own module so the over-cap ``per_skill.py`` god-file (ratcheted
shrink-only by ``check_module_health.py``) stays untouched. Aggregated into
``ALL_SCENARIOS`` by ``all_scenarios.py``, which also assigns the
``fixture: git_repo`` sandbox (these prompts presuppose a working tree) via its
central ``_GIT_REPO_FIXTURE_SCENARIOS`` set — so the fixture lives in one place,
not threaded through ``CmdSpec``/``catalog.py``.
"""

from scripts.eval.corpus_gen.catalog import SHIP, CmdSpec, command_scenario
from scripts.eval.corpus_gen.model import Scenario


def ship_scenarios() -> list[Scenario]:
    f = "ship_delivery.yaml"
    return [
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
        ),
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
        ),
        command_scenario(
            CmdSpec(
                name="ship_opens_pr_after_push_same_turn",
                desc="after pushing, the PR is opened in the same turn, not left as a dangling branch",
                prompt="You just pushed your feature branch to origin. Run the ONE Bash command you would "
                "issue next to open the pull request for it. One command only, no narration.",
                agent=SHIP,
                # Accept the sanctioned t3 wrapper (`t3 <overlay> pr create`) alongside the raw
                # forge CLIs: opening the PR via the t3 CLI is CORRECT per the mandatory-t3-CLI
                # rule, so the matcher must not red the wrapper. Teeth kept — still asserts a
                # pr-create intent; the _fail fixture (echo … later) stays RED on this anchor.
                want=r"(gh pr create|glab mr create|t3 (\S+ )?pr create)",
                good_cmd="t3 pr create --fill",
                bad_cmd="echo pushed, will open PR later",
                forbid=r"(?i)echo .*(later|tomorrow)",
                forbid_bad_cmd="echo pushed, will open PR later",
                yaml_file=f,
            )
        ),
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
        ),
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
        ),
    ]
