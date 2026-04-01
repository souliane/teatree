# Claude Code Internals — Patterns Relevant to TeaTree

Source analysis from the `Kuberwastaken/claurst` spec extraction (14 markdown
files documenting the full TypeScript + Rust architecture), cross-referenced
with `instructkr/claw-code` reference data and the v2.1.89 NPM bundle from
`Onewon/claude-code`. The `claurst` spec is the most authoritative source.

---

## 1. Skill Frontmatter — Complete Field Reference

Source: `claurst/spec/11_special_systems.md` § 4.4 (loadSkillsDir.ts)

The frontmatter parser recognizes these fields in `SKILL.md`:

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `name` | `string` | directory name | Display name. Falls back to parent dir name |
| `description` | `string` | first heading | Short summary (max 100 chars) shown in skill listing |
| `when-to-use` | `string` | — | Free-text shown after description in listing. The model reads this to decide when to invoke the skill |
| `user-invocable` | `bool` | `true` | If `false`, hidden from Skill tool listing |
| `disable-model-invocation` | `bool` | `false` | If `true`, excluded from listing — model can't auto-invoke, only manual `/skill` |
| `disableNonInteractive` | `bool` | `false` | Disables the skill in non-interactive (headless) mode |
| `model` | `string` | — | Override model for this skill. `"inherit"` = use parent |
| `effort` | `string\|int` | — | Override reasoning effort level |
| `allowed-tools` | `string\|string[]` | `[]` | Tools granted. `"*"` wildcard, brace expansion (`mcp__{a,b}__*`) |
| `argument-hint` | `string` | — | Hint shown in typeahead |
| `arguments` | `string\|string[]` | `[]` | Named arguments |
| `context` | `"inline"\|"fork"` | `"inline"` | `"fork"` = run as sub-agent in isolated context |
| `agent` | `string` | — | Associate with agent-based execution |
| `shell` | `"bash"\|"powershell"` | — | Shell for embedded commands |
| `hooks` | `object` | — | **Inline hooks** — skill registers its own hooks for any event |
| `paths` | `string\|string[]` | — | **Conditional activation** — glob patterns; skill stays dormant until a file op matches |
| `version` | any | — | Stored, not validated |

### BundledSkillDefinition TypeScript type

Source: `claurst/spec/11_special_systems.md` § 4.2

```typescript
type BundledSkillDefinition = {
  name: string
  description: string
  aliases?: string[]
  whenToUse?: string
  argumentHint?: string
  allowedTools?: string[]
  model?: string
  disableModelInvocation?: boolean
  userInvocable?: boolean
  isEnabled?: () => boolean
  hooks?: HooksSettings
  context?: 'inline' | 'fork'
  agent?: string
  files?: Record<string, string>  // extracted to disk on first use
  getPromptForCommand: (args, context) => Promise<ContentBlockParam[]>
}
```

### What TeaTree Invented (Not in Claude Code)

These are teatree-specific extensions:

- `triggers.keywords` — regex patterns for UserPromptSubmit hook matching
- `triggers.urls` — URL regex patterns
- `triggers.priority` — numeric ordering
- `triggers.exclude` — negative match regex
- `triggers.end_of_session` — end-of-session phrase detection
- `search_hints` — simple keywords for headless agent skill discovery

### How Claude Code Does Skill Matching

Claude Code has **no trigger/keyword system**. Skill discovery is model-driven:

1. Skills listed in a `<system-reminder>` as: `"- {name}: {description} - {when-to-use}"`
2. The model reads this listing and decides which to invoke
3. Conditional `paths` skills only appear after a matching file is touched
4. Budget-aware listing prevents context overflow from too many skills

---

## 2. Skill Loading Architecture

Source: `claurst/spec/11_special_systems.md` § 4.1–4.4

### Loading Sources (priority order)

1. **Managed/policy** (`~/.claude/skills/` managed dir)
2. **User settings** (`~/.claude/skills/`)
3. **Project settings** (`.claude/skills/` walking up to workspace root)
4. **Additional dirs** (`--add-dir`)
5. **Legacy commands** (`.claude/commands/`)
6. **Plugin skills** (marketplace or `--plugin-dir`)
7. **Bundled skills** (compiled into binary)
8. **MCP skills** (generated from MCP tool definitions)

### Content Template Variables

- `${CLAUDE_SKILL_DIR}` → actual skill base directory path
- `${CLAUDE_SESSION_ID}` → current session ID
- `$ARGUMENTS` → user-provided arguments
- `$ARG_NAME` → named arguments from `arguments` frontmatter
- Embedded shell: `` ```! cmd ``` `` or `` !`cmd` `` executed and output substituted

### File Extraction (`files` field)

Bundled skills with `files` get extracted to a per-process temp directory
(mode 0o600) on first use. Prompt is prefixed with
`"Base directory for this skill: <dir>"`.

---

## 3. Hook System — Complete Event List

Source: `claurst/spec/01_core_entry_query.md` (HOOK_EVENTS constant)

```typescript
const HOOK_EVENTS = [
  'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Notification',
  'UserPromptSubmit', 'SessionStart', 'SessionEnd', 'Stop', 'StopFailure',
  'SubagentStart', 'SubagentStop', 'PreCompact', 'PostCompact',
  'PermissionRequest', 'PermissionDenied', 'Setup', 'TeammateIdle',
  'TaskCreated', 'TaskCompleted', 'Elicitation', 'ElicitationResult',
  'ConfigChange', 'WorktreeCreate', 'WorktreeRemove', 'InstructionsLoaded',
  'CwdChanged', 'FileChanged',
] as const  // 27 events
```

### Hook Response Schema

Source: `claurst/spec/12_constants_types.md` § 24.4

Hook JSON output supports event-specific fields:

| Event | `hookSpecificOutput` fields |
|-------|---------------------------|
| `PreToolUse` | `permissionDecision`, `permissionDecisionReason`, `updatedInput`, `additionalContext` |
| `UserPromptSubmit` | `additionalContext` |
| `SessionStart` | `additionalContext`, `initialUserMessage`, `watchPaths` |
| `PostToolUse` | `additionalContext`, `updatedMCPToolOutput` |
| `PermissionRequest` | `decision` (allow w/ `updatedInput`/`updatedPermissions`, or deny) |
| `PermissionDenied` | `retry` |
| `CwdChanged`/`FileChanged` | `watchPaths` |
| `WorktreeCreate` | `worktreePath` |
| `Elicitation`/`ElicitationResult` | `action` (`accept`/`decline`/`cancel`), `content` |

### Skills Can Define Their Own Hooks

The `hooks` frontmatter field lets a skill register hooks for ANY event.
This makes skills self-contained — no separate `settings.json` configuration.

---

## 4. Context Compaction — Three Layers

Source: `claurst/spec/06_services_context_state.md` §§ compact, microCompact, autoCompact

### Layer 1: Microcompaction (lightweight)

Clears old tool result content without conversation summarization.

**Compactable tools:**

```typescript
const COMPACTABLE_TOOLS = new Set([
  FileRead, Shell, Grep, Glob,
  WebSearch, WebFetch, FileEdit, FileWrite
])
```

**The `Skill` tool is NOT in this set.** Skill content loaded via the Skill
tool cannot be microcompacted — it stays in context until full auto-compact.

Two paths:

- **Cached MC**: via API `cache_edits` — registers tool results, queues deletions
- **Time-based MC**: direct content mutation to `'[Old tool result content cleared]'`
  when idle gap exceeds threshold

### Layer 2: Auto-compact (full summarization)

- Triggers at ~90% of context window (configurable threshold)
- Calls full `compact()` — LLM summarizes the conversation
- Posts `CompactBoundaryMessage` as a marker
- `PreCompact` and `PostCompact` hooks fire before/after

### Layer 3: Reactive compact (emergency)

- Feature-gated (`REACTIVE_COMPACT`)
- Fires when prompt is too long for API
- Last resort before returning `blocking_limit` error

### Corrected Understanding

**The "5K tokens per skill" claim from the original tickets is not
substantiated.** Compaction replaces the entire conversation with a summary.
Skills are NOT individually truncated to 5K tokens. The correct concern is:

1. Skill content loaded via the Skill tool is **not microcompactable**
2. After full auto-compact, skill content is lost (replaced by summary)
3. The system prompt and `--append-system-prompt` survive compaction
4. Reference files loaded via Read **are** microcompactable

---

## 5. System Prompt Construction

Source: `claurst/spec/01_core_entry_query.md`, `claurst/spec/06_services_context_state.md`

### Layered Assembly

1. **Static CLI prefix**: `"You are Claude Code, Anthropic's official CLI"`
2. **Main instruction block**: tools, conventions, git, PR, security
3. **Environment info**: working dir, git status, platform, date, model
4. **Context blocks** (`<context name="key">value</context>`):
   - `directoryStructure`, `gitStatus`, `codeStyle` (CLAUDE.md files),
     `claudeFiles`, `readme`, user-set context
5. **System-reminder attachments**: skill listing, memory, etc.

### Prompt Caching

- System prompt split into 2 blocks with `cache_control: { type: 'ephemeral' }`
- Last 3 messages get cache breakpoints
- Any change in the prefix invalidates the cache

### Token Budget Tracking

```typescript
export function getCurrentTurnTokenBudget(): number | null
export function getBudgetContinuationCount(): number
```

Task-level token budgets are configurable via the `task-budgets-2026-03-13` beta header.

---

## 6. Headless / Print Mode

### `claude -p <prompt>`

- Non-interactive single-shot execution
- No permission UI — tools must be pre-approved
- `--dangerously-skip-permissions` only works in Docker without internet

### `--output-format json`

Structured JSON envelope on stdout.

### `--resume <session-id>`

Loads full conversation history for multi-phase workflows.

### `--append-system-prompt <text>`

Appended after main system prompt. Part of cached prefix.

---

## 7. Agent / Sub-Agent Patterns

Source: `claurst/spec/03_tools.md` (AgentTool), `claurst/spec/05_components_agents_permissions_design.md`

- No recursive agents (AgentTool filtered from sub-agent tools)
- Read-only by default unless `dangerouslySkipPermissions`
- Stateless — fresh context per invocation
- Sidechain logging to separate files
- `SubagentStart` and `SubagentStop` hooks fire
- Read-only tools run concurrently (up to 10)

---

## 8. Rust Codebase

Source: `claurst/spec/13_rust_codebase.md`

Claude Code includes a Rust crate (`cc-core`) that mirrors TypeScript types:

- `HookEvent` enum: `PreToolUse`, `PostToolUse`, `Stop`, `UserPromptSubmit`, `Notification`
- `run_hooks(hooks, event, context, working_dir) -> HookOutcome` (async)
- Tool execution: `PreToolUse` hooks fire → if `Blocked` → `ToolResult::error`
- `Skill` tool in Rust: reads `.md` files, strips YAML frontmatter, substitutes `$ARGUMENTS`

---

## 9. Key Corrections to Original Ticket Assumptions

| Original Claim | Reality (from claurst spec) |
|---|---|
| "Skills truncated to 5K tokens after compaction" | No per-skill truncation. Full compaction replaces entire conversation with summary. Skill content simply disappears. |
| "`search_hints` is a Claude Code pattern (like ToolSearch `searchHint`)" | `search_hints` is a teatree invention. Claude Code uses `when-to-use` free-text for model-driven discovery. ToolSearch `searchHint` is for *deferred tools*, not skills. |
| "Claude Code's `readFileState` cache tracks which files were read" | Confirmed: the Read tool requires prior reading before writes. Sub-agents get a cloned cache. |
| "Auto-compact triggers at contextWindow - 13,000 tokens" | Auto-compact triggers at ~90% of context window (configurable). The 13K figure is not confirmed. |
| "Hooks receive JSON on stdin with session_id, tool_name, tool_input" | Confirmed. Hook-specific output fields vary by event type (documented above). |
| "Prompt cache requires stable ordering" | Confirmed. System prompt split into 2 cached blocks. |
| "Skill tool content is not microcompactable" | **Confirmed.** `COMPACTABLE_TOOLS` does not include the Skill tool. |

---

## 10. Patterns TeaTree Should Adopt

### High Priority (directly applicable)

| Pattern | Source | Action |
|---------|--------|--------|
| `when-to-use` field | loadSkillsDir.ts | Add to teatree skills — model-driven discovery |
| `paths` conditional activation | loadSkillsDir.ts | Skills activate only when matching files touched |
| `hooks` in frontmatter | loadSkillsDir.ts | Skills carry their own hooks — self-contained |
| `allowed-tools` | loadSkillsDir.ts | Skills declare needed tools |
| Microcompact awareness | microCompact.ts | Route reference content through Read (compactable) not Skill (not compactable) |
| `PreCompact`/`PostCompact` hooks | HOOK_EVENTS | Persist state before compaction |

### Medium Priority

| Pattern | Source | Action |
|---------|--------|--------|
| `context: "fork"` | BundledSkillDefinition | Isolated skill execution |
| `model`/`effort` overrides | BundledSkillDefinition | Per-skill model selection |
| `disable-model-invocation` | BundledSkillDefinition | Control auto-discovery |
| `FileChanged`/`CwdChanged` hooks | HOOK_EVENTS | React to file changes |
| `SubagentStart`/`SubagentStop` hooks | HOOK_EVENTS | Track sub-agent lifecycle |
| `WorktreeCreate`/`WorktreeRemove` hooks | HOOK_EVENTS | Worktree lifecycle |
| `PermissionRequest` hook with `updatedInput` | Hook response schema | Modify tool input before execution |

### Low Priority (future consideration)

| Pattern | Source | Action |
|---------|--------|--------|
| MCP skills (from tool definitions) | LoadedFrom type | Generate skills from MCP tools |
| Plugin system | builtinPlugins.ts | Package teatree features as plugins |
| `files` extraction | BundledSkillDefinition | Ship reference files with skills |
| Time-based microcompact | microCompact.ts | Clear old results after idle gap |
