# Stacked delivery — one stack per repo

## Stacked Delivery — One Stack Per Repo (Default)

Applies to the GitLab-hosted factory fork (`glab stack`) and the GitHub-hosted teatree repo (`gh stack`). <!-- mcp-ratchet: allow — neither forge exposes stack topology through the teatree MCP surface. -->

Two regimes — never confuse them. Factory/teatree repos (agent-maintained; agents absorb the restack
toil): stack systematically, conflict-driven, per the steps below. Human-reviewed product repos: stack
only when the work genuinely decomposes into dependent layers AND the combined MR would be too big to
review (~40 changed files or ~1200 changed lines); size alone never mandates a stack; never stack
changes that could merge independently; keep stacks to 2-3 layers.

Before cutting a branch:

1. List open MRs/PRs and their changed files:
   - GitLab: `glab mr list` (opened is the default; there is no `--state` flag), then per MR
     `glab api "projects/:fullpath/merge_requests/<iid>/diffs?per_page=100" | jq -r '.[].new_path'` <!-- mcp-ratchet: allow — GitLab has no name-only MR-diff command; the read-only endpoint is the fallback. -->
   - GitHub: `gh pr list --state open`, then per PR `gh pr diff <n> --name-only` <!-- mcp-ratchet: allow — changed-file discovery must work when the forge MCP server is absent. -->
2. Compare with the paths this work will touch.
3. Overlap -> layer onto the repo stack: base the branch on the current stack TIP (the open stacked MR no
   other open MR targets) and set the ticket's repo-scoped `target_branch` override to that tip
   (`t3 <overlay> ticket set-target-branch <ticket> <owner/repo> <branch>`). A multi-repo ticket needs
   a separate override only for each repo that is actually layered; an override never crosses repos.
   No overlap -> branch off the default branch as usual.

One layer = one ticket. Never two tickets in one layer.

**HARD RULE — layer targets.** Each layer's MR/PR targets its PARENT branch, never the default branch.
After creating or syncing the stack, verify every layer's target; fix with
`glab mr update <iid> --target-branch <parent>` / `gh pr edit <n> --base <parent>`. <!-- mcp-ratchet: allow — live base retargeting has no t3 command and must work without a forge MCP server. --> A cumulative diff
means a wrong target branch — fix the target, do not re-review already-approved layers.

Operating limits: max 20 MRs per GitLab stack; auto-retarget covers at most 4 open MRs and fires only on
merge — verify targets after every merge; GitHub stacks are same-repo only — fork-based upstream
contributions stay single-PR.

CI cost: where CI gates on the MR target branch, only the BASE layer runs the expensive suite and upper
layers run a cheap subset — already true in the frontend product repo (expensive jobs gate on
`CI_MERGE_REQUEST_TARGET_BRANCH_NAME == master`), NOT yet in the backend product repo (it suppresses
non-train MR pipelines and runs feature work as branch pipelines, where the MR target is invisible to
CI rules — every layer runs the full suite there until a separate CI change lands). An upper-layer
pipeline must be CHEAP, never EMPTY: both projects set `only_allow_merge_if_pipeline_succeeds=true` and
`allow_merge_on_skipped_pipeline=false`, so a fully-skipped pipeline blocks the merge. Open each layer's
MR immediately after its first push — a layer branch pushed before its MR exists triggers a full branch
pipeline.

When the bottom layer merges, preserve every published commit: merge the updated target into the newly
retargeted layer, then walk bottom-up and merge each updated parent into its child once. Push each layer
normally and verify every target. Never use a stack sync/restack mode that rebases or force-pushes the
published chain.

**SAFETY RULE — the retarget hole.** When the base merges, the forge auto-retargets the next layer to
the default branch, but that layer's newest pipeline is still the cheap upper-layer one and satisfies
"pipeline must succeed" — it can merge having NEVER run tests or builds. After a retarget to the
default branch: merge the updated default branch into the layer AND push, as one step. The merge is
local — on its own it changes nothing on the forge and starts no pipeline; the push is what creates the
fresh FULL pipeline. From that layer's worktree the recovery is the single command
`git fetch origin <default> && git merge origin/<default> --no-edit && t3 push`. Then wait for the
fresh FULL pipeline before merging. Never rebase or force-push the published layer, and never merge a
retargeted layer on its upper-layer pipeline.

If a stack CLI command fails: branch-on-parent plus
`glab mr update <iid> --target-branch <parent>` / `gh pr edit <n> --base <parent>` gives the same chain. <!-- mcp-ratchet: allow — live base retargeting has no t3 command and must work without a forge MCP server. -->
