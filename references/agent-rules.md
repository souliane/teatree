# Agent Rules

> Cross-cutting rules that apply to all skills. Referenced from individual skill files.

---

## Clickable References (Non-Negotiable)

Every MR, ticket, issue, or note reference — in markdown files, platform comments, **and** agent responses — must be a clickable markdown link.

- `[!5657](https://example.com/org/repo/-/merge_requests/5657)` — not `!5657`
- `[PROJ-1234](https://example.com/org/repo/-/issues/1234)` — not `PROJ-1234`

This applies everywhere: MR/PR descriptions, inline comments, test evidence, chat messages, and responses to the user.

## Token Extraction (Non-Negotiable)

When extracting an API token from a CLI tool, always extract to a variable first — never inline in curl. See your [issue tracker platform reference](platforms/) § "Token Extraction" for the platform-specific recipe.

**In Python heredocs:** shell variables are NOT inherited. Use `os.popen(...)` inside Python or `export TOKEN` before the heredoc.

## Temp File Safety (Non-Negotiable)

When using temporary files (for MR note bodies, test data, etc.):

- **Never use hardcoded paths** like `/tmp/mr_note_body.md` — stale content from other sessions gets posted to the wrong MR.
- **Always use `mktemp`** or inline Python heredocs instead.
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite.
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).

## Complex API Payloads: Use curl or Python (Non-Negotiable)

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

See your [issue tracker platform reference](platforms/) § "Known CLI Quirks" for platform-specific details.

## Never Post MR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on MRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments. Post all MR comments from the main conversation thread, or serialize agent tasks so only one posts at a time.

## Sub-Agent Limitations (Non-Negotiable)

Sub-agents (Agent tool) **lose all loaded skills, MCP access, and shell functions**. By default, never dispatch sub-agents for skill-dependent work. Do all skill-dependent work sequentially in the main conversation.

**Exception:** Skills with `subagent_safe: true` in their YAML frontmatter are pure methodology/guidelines that work without shell functions, MCP tools, or cross-skill state. When delegating to a sub-agent with a safe skill, **pass the full SKILL.md content in the sub-agent prompt** — sub-agents cannot load skills themselves.

Skills without `subagent_safe` in their metadata default to `false` (unsafe).

## Verification Before Claims (Non-Negotiable)

No completion claims without fresh verification evidence. See `t3-test/SKILL.md` § "Verification Before Claims" for the full evidence table and screenshot sanity checks.

## Definition of Done (Non-Negotiable)

"Done" means **re-running the same skill produces zero new findings**. This applies to any skill that audits, reviews, or improves (retro, review, quality checks). Before claiming done:

1. **Re-run the skill mentally or literally** on the same scope.
2. If the re-run would produce new findings, you are not done — fix them first.
3. Only claim done when a re-run is idempotent (zero new issues).

This is not optional polish — it is the exit condition. The user should never have to ask for a verification pass.

**Proactively declare done.** When all work is complete and verified, state "Done." with a concise summary of what was changed. Do not wait for the user to ask "all done?" — that means you failed to communicate completion.

## Shell Config Loading (Non-Negotiable)

The agent's shell execution tool (e.g., Bash tool in Claude Code) does not inherit the user's interactive shell environment. Teatree config variables (`T3_REPO`, `T3_OVERLAY`, `T3_CONTRIBUTE`, `T3_WORKSPACE_DIR`, etc.) live in `~/.teatree` and must be **explicitly sourced** before use:

```bash
source ~/.teatree
```

**Always source `~/.teatree`** as the first command in any Bash invocation that reads teatree variables. Do not assume variables are already in the environment — each shell command invocation starts a fresh shell.

## Config File Safety (Non-Negotiable)

When modifying system or dev environment config files (git, shell, IDE, agent config) that are NOT workspace code:

1. **List every file added or modified** with full paths so the user can back them up.
2. **Never edit dotfile management scripts** (e.g., `setup.sh`, `install.sh`) without explicit consent — suggest additions instead.

This applies to: `~/.gitconfig`, `~/.zshrc`, `~/.config/`, agent-specific config directories, and any other dotfile or system config.

## Symlink Safety (Non-Negotiable)

**Never** use `rm` + recreate, `cp`, `rsync`, or any operation that would **replace a symlink with a real file/directory**. If unsure whether a path is a symlink, run `ls -la` first. This is critical for skill directories and worktree structures where symlinks are the intended mechanism.

### `ln -sfn` vs `ln -sf` (Critical Difference)

When the target path already exists as a symlink pointing to a directory, `ln -sf` creates the symlink INSIDE the directory. Use `ln -sfn` (no-dereference) to replace the symlink itself:

```bash
# WRONG — creates symlink INSIDE the directory
ln -sf /new/target ~/.agents/skills/ac-multitask

# RIGHT — replaces the symlink itself
ln -sfn /new/target ~/.agents/skills/ac-multitask
```

Recovery when a symlink was replaced by a real directory: `rm -rf <path> && ln -sfn <target> <path>`, then verify with `file <path>` and `readlink <path>`.

## Shell Alias Safety (Non-Negotiable)

In zsh, common commands like `rm`, `cp`, and `mv` are often aliased to interactive variants (`rm -i`, `cp -i`, `mv -i`). These aliases cause shell commands to **hang indefinitely** waiting for confirmation the user can't provide. Always use `command rm`, `command cp`, `command mv` to bypass shell aliases when the operation is non-interactive.

## Edit Tool `replace_all` Safety (Non-Negotiable)

Never use `replace_all: true` when the target string is an inline comment (e.g., `# noqa: S603`) at the end of a code line. The replacement deletes across ALL occurrences, which can merge code with the next line if the comment was the only separator — causing silent syntax corruption. Always use targeted (non-replace-all) edits for inline comments, including surrounding code context.

## External Service Access Priority (Non-Negotiable)

Before claiming "I don't have access to X", check available integrations. Priority chain: **CLI tools > platform integrations (Slack, Notion, Sentry MCP) > user-configured MCP servers > workarounds**. Search for the service name in the deferred tools list with ToolSearch. If it needs authentication, tell the user to connect it rather than skipping.

## Intellectual Consistency (Non-Negotiable)

When you've already analyzed a question and reached a conclusion, **don't reverse your position just because the user asks again or phrases it differently**. Re-evaluate independently — if the new framing reveals genuinely new information, update your position and explain what changed. If it doesn't, restate your original reasoning. Flipping positions to match the user's apparent preference destroys trust and wastes time.

## Verify File State After Context Compaction (Non-Negotiable)

After a context compaction event, **always re-read files before editing them**. Compaction loses intermediate file states — a file you edited earlier in the session may already contain your changes, and editing without reading first creates duplicates or conflicts.

## 2-Minute Troubleshooting Time-Box (Non-Negotiable)

If environment setup or test execution hits unexpected errors, do NOT spend more than 2 minutes troubleshooting. Instead: stop, check the loaded skills for documented solutions, and if not found, ask the user with specific details. The skills document every known failure mode — if you're improvising, you missed a skill.

## Skill File Writes Require a Git Repo (Non-Negotiable)

**Never modify a skill file that is not inside a git-tracked repository.** Changes to non-git-tracked copies are silently lost — there is no mechanism to recover, push, or share them.

Before writing to any skill file (including overlays, references, scripts, hooks):

1. **Resolve the real path:** `readlink -f <path>` (follows symlinks to the actual file).
2. **Verify it's in a git repo:** `git -C "$(dirname "$(readlink -f <path>")")" rev-parse --git-dir >/dev/null 2>&1`
3. **If the check fails, STOP.** Inform the user: "This skill is not in a git repository — changes would be lost. Run `/t3-setup` to fix skill symlinks."

This applies to **all skill modification workflows**: `/t3-retro` (overlay + core improvements), the review skill (if `T3_REVIEW_SKILL` is configured), and any manual skill edits.

## Skill Ownership Check (Non-Negotiable)

**Before modifying any skill file, verify the user maintains it.** Skills may be shared repos with multiple contributors — modifying someone else's skill creates unwanted changes.

After resolving the real path (step 1 above), check it against the `MAINTAINED_SKILLS` regex from the skill ownership file:

```bash
real_path=$(readlink -f "<skill_file>")
ownership_file="${T3_SKILL_OWNERSHIP_FILE:-$HOME/.ac-reviewing-skills}"
owned_pattern=$(grep '^MAINTAINED_SKILLS=' "$ownership_file" 2>/dev/null | cut -d'"' -f2)
if [ -n "$owned_pattern" ] && ! echo "$real_path" | grep -qE "$owned_pattern"; then
  echo "STOP: skill not owned by user — ask before modifying"
fi
```

- **Match → proceed** (user maintains this skill).
- **No match → ASK the user** before modifying. Suggest writing to a file the user owns instead (e.g., overlay references, repo `AGENTS.md`).
- **No ownership file → ASK** for any skill outside `$T3_REPO` and `$T3_OVERLAY`.

The ownership file (`$T3_SKILL_OWNERSHIP_FILE`) is shared with the review skill (configured via `T3_REVIEW_SKILL`). See `/t3-setup` Step 8 for how to generate it.

## Pitfalls Must Fix Examples, Not Just Document (Non-Negotiable)

When adding a pitfall or "don't do this" warning to a skill, **grep the entire skill for existing code examples that use the bad pattern** and fix them. A pitfall that says "don't use X" while procedure §3 teaches X creates a contradiction — agents follow examples over warnings. After adding any pitfall, run: `grep -n '<bad_pattern>' SKILL.md` and fix every hit that isn't inside the pitfall section itself.

## Skill Abstraction Boundaries (Non-Negotiable)

Core teatree skills (`t3-*`) and generic skills must **never** reference a specific project or overlay by name. Project-specific knowledge — repo names, tenant mappings, CI quirks, domain terminology — belongs exclusively in the project overlay (`$T3_OVERLAY`). User-specific preferences (default branding, paths, usernames) belong in the user's memory/config files.

If a core skill needs project context at runtime, it should call an extension point or read a config variable — not hardcode project details.

## Ask the User Clearly (Non-Negotiable)

When a skill says "ask the user", use the agent platform's native question or confirmation UI if it has one. If it does not, ask plainly in the conversation text.

- **One question at a time.** Never present a list of questions all at once — ask the first question, wait for the answer, then ask the next. This applies even when a skill lists multiple questions in sequence (the list defines *what* to ask, not that they should be asked simultaneously). Auto-detect and propose defaults wherever possible to minimize the number of questions the user actually needs to answer.
- Prefer structured prompts when the platform supports them
- Fall back to a direct written question when there is no dedicated question tool
- Claude Code example: `AskUserQuestion("Which tenant should I target?", ["customer-a", "customer-b", "detect automatically"])`

## Where to Persist Information (Non-Negotiable)

Agents have several places to store information. Using the wrong one causes rules to silently not load, or private details to leak into public repos. **When unsure, ask the user.**

### Decision Table

| What you're storing | Where it goes | Why |
|---|---|---|
| **Rules that apply to ALL repos** (commit style, formatting, workflow) | Global agent config (e.g., `~/.claude/CLAUDE.md`) | Loaded in every conversation, every repo |
| **Project-specific facts** (repo layout, secrets, tenant names, infra) | Agent memory (e.g., `~/.claude/projects/<project>/memory/`) | Scoped to conversations started from that directory |
| **Guardrails, patterns, troubleshooting** that help other users too | Skill files (`SKILL.md`, `references/`) | Available to anyone who installs the skill |
| **User preferences** (tone, tool choices, personal workflow) | Global agent config or memory | Depends on scope — global if it applies everywhere |

### Key Differences

- **Global config** (`~/.claude/CLAUDE.md`): loaded in EVERY conversation regardless of working directory. Use for universal rules. Agents treat these as top-priority instructions.
- **Project memory** (`~/.claude/projects/<encoded-path>/memory/`): loaded only when the working directory matches the encoded path. Use for project-specific knowledge that shouldn't pollute other contexts.
- **Skill files**: loaded only when the skill is explicitly loaded (via hook or user request). Use for reusable patterns and guardrails. Must be generic — no user-specific or project-specific details in public skills.

### Common Mistakes

- Putting a universal rule in project memory → it doesn't load in other projects.
- Putting project-specific details in a public skill → leaks private information.
- Putting a rule only in a skill → it's not enforced until the skill is loaded, which may be too late.
- Duplicating between memory and skills without marking the source → entries drift apart silently.

**When in doubt:** ask the user "Should this rule apply everywhere or just in this project?" and "Should other users of these skills benefit from this too?"

## Prefer Native Tool APIs Over Filesystem Heuristics (Non-Negotiable)

When a tool provides a built-in command (e.g., `git worktree list`, `docker ps`, `glab auth status`), always use that instead of filesystem patterns, `compgen`, or `find` heuristics. Built-in commands are authoritative and handle edge cases (stale state, platform differences) that heuristic scans miss.

## Respect the User's Stated Plan (Non-Negotiable)

When the user specifies a delivery plan (e.g., "two separate MRs", "dedicated branch for the refactoring", "fix first, then feature"), deliver exactly that — even if combining feels simpler. The user may have reasons you can't see: review ordering, dependency chains, rollback granularity, or team conventions.

If you believe a different plan would be better, **state your reasoning and wait for approval** before deviating. Never silently merge, split, reorder, or skip deliverables that the user explicitly defined.

**Separate refactoring from feature work:** When a ticket involves both a refactoring (not directly related to the ticket's goal) and a feature/fix, the refactoring should go in a dedicated MR — even if it's the same repo and same ticket. If the refactoring is a prerequisite for the feature, deliver it first and base the feature MR on it. If the refactoring is opportunistic cleanup discovered during implementation, deliver it after the feature MR. Either way, keep MRs focused — don't mix unrelated refactoring into a feature MR.
