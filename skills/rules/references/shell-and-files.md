# Shell and files — secrets, temp files, publish commands, and probes

The full text of the `/t3:rules` sections on shell and filesystem safety: secrets, tokens, temp files, API payloads, publish commands, remote databases, native APIs and MCP tools, symlinks, tracked dotfiles, session crons, aliases, and zsh probes. The core `skills/rules/SKILL.md` carries each Non-Negotiable rule's trigger and verdict.

## Read Secrets From the Secret Store (Non-Negotiable)

Every credential — API token, service password, signing key — is read **from the secret store at point of use**, never hard-coded in a command, a file, a commit, or echoed into the transcript. The canonical fetch is a secret-manager read into a variable, so the literal value never appears in your tool call or in shell history.

Do X — read from the store:

```bash
TOKEN="$(pass show <service>/api-token)"     # password-store
# or: TOKEN="$(op read 'op://<vault>/<item>/token')"   # 1Password CLI
# or: TOKEN="$(vault kv get -field=token <path>)"        # HashiCorp Vault
```

Never Y — never inline or echo a literal secret (the `<...>` below stands in for the real value, which must never appear):

```bash
export SERVICE_TOKEN=<the-literal-token>        # FORBIDDEN — literal in history + transcript
curl -H "Authorization: Bearer <the-literal-token>"   # FORBIDDEN — literal in the command
```

Reference the variable (`"$TOKEN"`) in the call that needs it; never the literal. See `t3:platforms` § "Token Extraction" for the per-platform CLI recipe. Pinned by `evals/scenarios/privacy_and_safety.yaml` (`safety_secret_read_from_secret_store`).

## Token Extraction

When extracting an API token from a CLI tool, always extract to a variable first — never inline in curl. See your platform reference (`t3:platforms`) § "Token Extraction" for the platform-specific recipe.

**In Python heredocs:** shell variables are NOT inherited. Use `os.popen(...)` inside Python or `export TOKEN` before the heredoc.

## Temp File Safety

When using temporary files (for PR note bodies, test data, etc.):

- Hardcoded paths are forbidden like `/tmp/mr_note_body.md` — stale content from other sessions gets posted to the wrong PR.
- **Always use `mktemp`** or inline Python heredocs instead.
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite (an instance of § "Shell Probes Run Under zsh").
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).
- **Exception: pre-compaction snapshots** — files matching `/tmp/t3-snapshot-*.md` are recovered automatically on the post-compaction `SessionStart` (`source=="compact"`) event (issue #845). Use `t3-snapshot-${CLAUDE_SESSION_ID:-manual}-$(date +%Y%m%d-%H%M).md` for the filename. Delete after persisting findings to durable storage.

## Complex API Payloads: Use curl or Python

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

## Never Pipe, Redirect, or Chain a gh/glab Publish Command

The banned-terms (#1415) and quote-scanner (#1213) gates classify a command's visibility by walking EVERY top-level `&&`/`;`/`|`/newline segment — a segment carrying any redirection, heredoc, or substitution construct (`>`, `<<`, `2>&1`, `$(...)`) forces a conservative SCAN of the whole command, even when the actual `gh`/`glab` post targets a known-private repo that would otherwise skip. This is by design (an unrecognised construct could hide a second, unverifiable command), but it means a habitual `... 2>&1 | python3 -c "..."` tacked onto a `gh pr create`/`glab api` call — or writing a body file via heredoc in the SAME Bash call as the post — reliably trips the gate on a repo that is genuinely private.

**Issue the publish command ALONE, in its own Bash call, with no trailing `2>&1`, no pipe, no heredoc.** Write any body file in a separate, prior Bash call; inspect the JSON response (if needed) in a separate, later call. Splitting the call costs nothing and avoids a false block that has nothing to do with the destination's actual visibility.

## Never Modify a Remote Database Without Explicit User Approval (Non-Negotiable)

Never write to, mutate, seed, or delete data in a remote/shared database (dev, staging, production, or any environment the agent did not provision locally) without explicit user approval in the chat for that specific action. This covers direct SQL/`psql`, ORM shells against a remote `DATABASE_URL`, seed/fixture scripts pointed at a remote DB, and API calls whose side effect is a remote write performed solely to set up the agent's own task. Read-only queries are fine. Generating a document or other persisted record on a remote environment is a remote write — ask first. A request to "finish the task" or "get the evidence" is not approval to mutate a shared DB; surface the blocker and let the user decide.

**Testing carve-out (dev only).** When running E2E or other tests against a **dev** environment, creating the agent's own task data — new loan requests, offers, documents, fixture rows — is allowed without per-action approval. This carve-out exists because dev is a testing environment and undeployed work must still be E2E-tested end to end. It is bounded: never mutate, reassign, or delete objects the agent did not itself create (no hijacking other people's records), never run destructive or bulk operations, and never touch staging or production under this carve-out — those still require explicit approval as above. When the dev testing carve-out applies it takes precedence over the general "ask first" rule for the agent's own test-scoped writes; when in doubt about whether a write is test-scoped and self-owned, fall back to asking.

## Prefer Native Tool APIs Over Filesystem Heuristics

When integrating with tools (issue trackers, CI, chat), prefer their API or CLI over scraping files. File-based approaches break on layout changes, don't handle pagination, and miss metadata.

## Prefer the Teatree MCP Tools Over the `t3` CLI

When an operation is served by a teatree MCP tool, **call the tool**. The `t3` CLI is the fallback, not the default. The reads return structured JSON — no text parsing, no scraping of a human-readable table that changes shape — and every write tool wraps the exact seam the CLI calls, so the same FSM / merge / on-behalf / leak gates fire either way.

**An MCP write is never a gate bypass.** `pr_create` runs the full ship-gate chain, `pr_merge` is sha-bound and maker≠checker enforced, `review_post_comment` is DRAFT unless the recorded LivePostApproval exists. Reaching for the CLI to dodge a gate the tool enforces is the same refusal, reached later.

**Where the tools come from.** A main session gets them from the plugin-bundled `.mcp.json` (`t3 mcp serve`). The headless dispatch injects that same stdio server into its own lifecycle sub-agents, so a dispatched coder / reviewer / shipper has them too — Claude Code forwards neither a local stdio server nor a plugin sub-agent's `mcpServers` frontmatter, so that wiring is explicit and deliberate. A general-purpose `Agent` sub-agent does NOT inherit one unless its definition declares it; that agent uses the CLI.

**The CLI is the right call when:**

- the MCP server is not connected — say so, never silently degrade a failed read to an empty result, or
- no tool covers the operation — worktree/workspace provisioning, the `run` / `e2e` / `db` lanes, `doctor`, `setup`, `update`, `push`, `eval`, the loop verbs, and issuing a merge CLEAR (`ticket clear`), or
- the tool refuses by design — `config_setting_set` refuses safety-gate keys (`*_gate_enabled`, `require_*`, feature flags, authorization/allowlist keys), which stay human/CLI-only.

Tools surface as `mcp__teatree__<name>`. When no tool covers an operation, `command_search` answers which `t3` leaf command does.

## Symlink Safety

Never replace a symlink with a real file. `ls -la` first if unsure. If a path is a symlink, edit the target — never delete the link and write a new file.

## Read Before Overwriting a Tracked Config/Dotfile (Non-Negotiable)

A user config file or dotfile (a `dotfiles`-repo file, an XDG `.config` file, `.zshrc`, …) is **authoritative as it exists on disk right now** — even when that on-disk content diverges from the committed version. The user may have made uncommitted edits directly on disk. So before you clobber it you must **read its current content this session**:

- A full **`Write`** that overwrites an existing config/dotfile, OR a **`git checkout` / `git restore`** that restores a tracked config from a committed version, discards the live on-disk content. Do **not** do either blind — `Read` the file first, confirm what you intend to change, then re-issue the write.
- **Uncommitted-on-disk beats committed.** Never "restore the config from git to a clean state" without first reading the working-tree copy — the committed version is NOT the source of truth for a user config; the file on disk is.
- This is the file-write sibling of § "Read the Canonical Source Before a Structural Action" and § "Read the Canonical Source Before Fixing a Conformance Bug": the live artifact is the spec; read it before you act on it.

**Deterministically enforced.** The PreToolUse gate `handle_block_config_overwrite` (`hooks/scripts/config_overwrite_guard.py` + `teatree.core.gates.config_overwrite_guard`) refuses a blind `Write` over an existing config/dotfile and a blind `git checkout`/`git restore` of one when the path was not read this session (it consumes the existing `<session>.reads` capture). Reading the file first clears it. Never-lockout escapes: a per-call `[config-overwrite-ok: <reason>]` token, the `[teatree] config_overwrite_gate_enabled = false` kill-switch (`t3 <overlay> gate config-overwrite disable`), and the shared `_fail_open_or_deny` chain.

**Failure mode this prevents.** An agent overwrote a tracked dotfile (a symlink into the user's dotfiles repo) with a blind `Write`, and on another occasion nearly restored a config from git without reading the live copy — both would have silently destroyed the user's uncommitted edits.

## Never Cron a `t3 loop` Command From a Session (Non-Negotiable)

The `t3 worker` owns loop cadence. A harness cron / `/loop` / `ScheduleWakeup` that shells a `t3 loop` command the worker already drives is pure waste — every tick stands down against the worker singleton, and `retired=0 drained=0` is the **expected healthy output**, not a signal. The first occurrence cost ~40 turns firing a guard declining to fire.

**Check the worker BEFORE honouring a session-setup ask — do X, never Y.** The session-setup hook that asks you to register `slack-answer` / `self-improve` / `drain-queue` describes a **pre-flip box**. On a box whose worker is alive, honouring it is waste.

```bash
# do X — check first; a RUNNING worker means register nothing:
t3 worker status          # `worker: RUNNING (pid 7)` → the worker drives all three; stop here
t3 worker ensure          # not running? this is the fix — not a cron
t3 loop enable <name>     # a genuinely needed loop becomes a DB `Loop` row, the normal t3 way
# never Y — a cron/wakeup that shells a loop command the worker owns:
#   CronCreate(prompt="Run `t3 loops tick --loop dispatch`")   ← FORBIDDEN: PR-28 retired the cron mirror
#   /loop 30s Run `t3 loop drain-queue run`.                   ← FORBIDDEN while a worker is alive
```

All three reactive slots run as **worker maintenance chains** (`teatree.loops.timer_reconciler`), so a live worker drives them with no session open. A session registers them only when no worker is alive — the legitimate degraded path, and the only case the gate lets through.

Standing DIRECTIVES (`standing-pr-board`, `standing-todo-consolidate`) are a different thing and DO belong to the session — prose delivered on a cadence, not loops.

**Deterministically enforced.** The PreToolUse gate `handle_block_cron_loop_shell` (`hooks/scripts/cron_loop_shell_gate.py` + `teatree.core.gates.cron_loop_shell_gate`) refuses a `CronCreate` / `ScheduleWakeup` whose prompt shells `t3 loops tick …` (always — there is no fallback plane) or `t3 loop <slot> run` (while a worker holds the singleton). Never-lockout escapes: a per-call `[cron-loop-ok: <reason>]` token, the `[teatree] cron_loop_shell_gate_enabled = false` kill-switch (`t3 <overlay> gate cron-loop-shell disable`), and the shared `_fail_open_or_deny` chain. The behavioural pin is `evals/scenarios/no_session_crons_for_t3_loops.yaml`.

**Failure mode this prevents.** This rule had a durable memory and was violated three times (2026-08-11, 08-13, 09-06), each time after memory decay archived the file — which is exactly why the remediation is a gate, not another memory.

## Shell Alias Safety

Use `command rm`, `command cp`, `command mv` in Bash tool calls to avoid zsh interactive aliases that hang. Also `gs` is aliased to `git status` — use `command gs` for GhostScript. (An instance of the general fact below: the Bash tool's shell is zsh.)

## Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable

**The Bash tool's shell is zsh. bash idioms do not error here — they answer WRONGLY.** State this once and generalize it: the two notes above (`>|` in § "Temp File Safety", `command rm` in § "Shell Alias Safety") are instances of this one fact, not isolated trivia. An agent writing a shell **probe** — a throwaway command to check whether some property holds — reads neither of those sections (it is not writing a temp file, not calling `rm`), writes bash out of habit, and gets **confident, meaningless output instead of an error**. The four ways this has actually broken a probe:

| Bash idiom | What zsh actually does |
| --- | --- |
| `${BASH_SOURCE[0]}` | **Empty.** `cd "$(dirname "${BASH_SOURCE[0]}")"` silently resolves to `dirname ""` → `.` → **cwd**, so the probe "works" against the wrong directory. |
| `for x in $var` (unquoted) | **No word-splitting** in zsh. The loop iterates **once**, over the whole string, and reports one clean pass. |
| `> file` on an existing stub | `noclobber` silently blocks the rewrite. The probe reads back the **stub's old content** and concludes the property holds. |
| `grep -r <pat>` with no file arg | Recurses **cwd** instead of reading stdin. Returns matches from the tree, not from the piped input under test. |

**A pipeline reports the LAST command's status, so `cmd | tail` reports `tail`'s.** Piping a command through `tail`/`grep`/`head` to trim its output discards its exit code: a command that failed loudly reads as success, and its own error text is often the part trimmed away. Capture the real status (`${PIPESTATUS[0]}`, or run the command unpiped and trim afterwards) whenever the status is what you are about to reason about. Same failure shape as the rows above, and it survives being read twice in one session because the transcript shows a clean-looking result either way.

**A probe that SPAWNS shells owns their process group, and every generated case gets a timeout (Non-Negotiable).** A throwaway fuzz/probe harness inherits the caller's process group, so when it dies its children keep running with no parent, no owner and no reaper. One such group ran 9 days 10 hours on this box and removed ~58% of the factory's admitted capacity — near-idle on CPU, but a tight loop is _runnable_, and runnable is what the load average the admission governor throttles on counts. A generated payload that corrupts the loop's own exit keyword into an infinite loop is a NORMAL fuzzer outcome, not an exceptional one, so both halves are mandatory:

```bash
# do X — the harness leads its own group and kills the GROUP in a finally, on every exit path:
#   proc = spawn_session_leader([...])          # teatree.utils.run — start_new_session=True
#   try: ...                                    # or run_deadlined_argv (deadline + killpg)
#   finally: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
# never Y — a bare spawn with no group and no per-case deadline:
#   subprocess.Popen(["sh", "-c", generated])   # FORBIDDEN: orphans outlive the harness
```

`t3 doctor check` reports a leaderless group that is still burning, and `t3 tool reap-orphan-groups --pgid <n> --apply` reclaims one — but the harness is what stops it existing. (Issue [#4580](https://github.com/souliane/teatree/issues/4580).)

**A timeout is not a failure.** When a foreground call hits its time limit, the process it started is usually still running. Launching a second attempt then puts two writers on one resource — the second fails on a lock the first legitimately holds, and that lock reads as stale debris. Before retrying anything that timed out, check whether the first attempt is still alive; before clearing a lock, confirm no live process owns it.

Every row fails the same way: **the wrong answer looks exactly like the right answer** — nothing errors, nothing is empty, the output is well-formed and false. (The `noclobber` case is the proof a scattered symptom-fix does not work: that exact rule is already written under § "Temp File Safety", and the failure still recurred, because no probe author looks there.)

**Always include a CONTROL proving the probe can detect what it looks for (Non-Negotiable).** Plant the violation, or run the old code, and confirm the probe goes **RED** before trusting a GREEN. A probe with no control cannot distinguish "the property holds" from "my harness is broken" — both present as GREEN. This is what turns each zsh row above from a silent bug into a _reported finding_: the loop that iterated once, the read-back of a stale stub, the grep against the wrong tree — every one returns a green a control would have caught in one extra command. The two halves are one rule: the shell lies quietly, so a green needs a control before it is evidence. (Issue [#3363](https://github.com/souliane/teatree/issues/3363).)
