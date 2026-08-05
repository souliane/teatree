# The privacy scan — what to scan, what to scan for, and the strictness levels

The procedure behind `/t3:retro` § "Privacy Scan". That section carries the scope rule (every public-facing artifact this session, not just the staged diff); this file carries the four surfaces, the detector set, and the `T3_PRIVACY` levels.

## What to scan

1. **Full branch-vs-base diff, not just the current session's hunks.**

    ```bash
    git -C "$T3_REPO" diff @{upstream}..HEAD | t3 tool privacy-scan -
    ```

    The branch may carry older commits from prior sessions or compacted work that the agent never re-read. `git diff @{upstream}..HEAD` covers every commit between the pushed base and HEAD. `git diff --cached` or `git diff HEAD~..HEAD` is **not enough** — it only shows the most recent work.

2. **Commit subjects and bodies on the branch.**

    ```bash
    git -C "$T3_REPO" log --format='%H %s%n%b' @{upstream}..HEAD | t3 tool privacy-scan -
    ```

    Commit messages are public and indexed. A subject that names what was scrubbed leaks the fact of the scrub even when the diff itself is clean.

3. **PR, issue, and comment bodies the agent has written this session.** Before declaring retro complete, grep every published artifact — PR descriptions you authored, PR/issue comments you posted, release notes, changelogs, and the branch name itself. Internal IPs, `/Users/…` paths, customer names, ticket IDs, or class-of-data words can slip in here even when the code diff is clean.

4. **Memory and config files written this session.** Fresh memory writes to `MEMORY.md` or per-memory files can repeat a leaked string verbatim ("the leaked value was `…`"). Reference the incident without reproducing the string.

## What to scan for

Run the standard `t3 tool privacy-scan` detectors (emails, `/Users/` and `/home/` paths, private IPs, API keys `glpat-` / `sk-` / `ghp_`, internal hostnames, and `T3_BANNED_TERMS`).

In addition, when the session involved remediating a leak, grep the Streisand-effect word list from `rules/SKILL.md` § "Leak Remediation — Silent Scrubs":

```text
leak|scrub|redact|real|private|personal|sensitive|accident|phone|email|password|token|credential|secret|address
```

A hit on those words in a commit subject, branch name, or public comment means the remediation itself amplifies the leak — rewrite or delete before declaring done.

## `T3_PRIVACY` levels

- **`strict`** (default): Exit 1 on ANY finding. Require user to manually resolve before proceeding.
- **`relaxed`**: Warn on findings but exit 0. Pass `--no-strict` to the script.

When `T3_PRIVACY` is not set, default to `strict`.
