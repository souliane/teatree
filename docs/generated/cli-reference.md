# CLI Reference

Generated from `t3` command tree.

## `t3`

```
Usage: t3 [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ startoverlay    Create a new TeaTree overlay package.                        │
│ docs            Serve the project documentation with mkdocs.                 │
│ agent           Launch Claude Code with auto-detected project context.       │
│ sessions        List recent Claude conversation sessions with resume         │
│                 commands.                                                    │
│ info            Show t3 installation, teatree/overlay sources, and editable  │
│                 status.                                                      │
│ dashboard       Migrate the database and start the dashboard dev server.     │
│ config          Configuration and autoloading.                               │
│ ci              CI pipeline helpers.                                         │
│ review          Code review helpers.                                         │
│ review-request  Batch review requests.                                       │
│ doctor          Smoke-test hooks, imports, services.                         │
│ tool            Standalone utilities.                                        │
│ setup           First-time setup and global skill management.                │
│ assess          Codebase health assessment.                                  │
│ infra           Teatree-wide infrastructure services.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 startoverlay`

```
Usage: t3 startoverlay [OPTIONS] PROJECT_NAME DESTINATION

 Create a new TeaTree overlay package.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    project_name      TEXT  [required]                                      │
│ *    destination       PATH  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --overlay-app            TEXT  Name of the overlay Django app                │
│                                [default: t3_overlay]                         │
│ --project-package        TEXT  Project package name (default: derived from   │
│                                project name)                                 │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 docs`

```
Usage: t3 docs [OPTIONS]

 Serve the project documentation with mkdocs.

 Requires the ``docs`` dependency group: ``uv sync --group docs``

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --host        TEXT     Host to bind to [default: 127.0.0.1]                  │
│ --port        INTEGER  Port to serve on [default: 8888]                      │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 agent`

```
Usage: t3 agent [OPTIONS] [TASK]

 Launch Claude Code with auto-detected project context.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   task      [TASK]  What to work on (e.g. 'fix the sync bug', 'add a new     │
│                     command')                                                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --phase        TEXT  Explicit TeaTree phase override.                        │
│ --skill        TEXT  Explicit skill override. Repeat to load multiple        │
│                      skills.                                                 │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 sessions`

```
Usage: t3 sessions [OPTIONS]

 List recent Claude conversation sessions with resume commands.

 By default shows sessions for the current working directory.
 Use --all to show sessions across all projects.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --project          TEXT     Filter by project dir substring                  │
│ --limit    -n      INTEGER  Max sessions to show [default: 20]               │
│ --all      -a               Show sessions from all projects                  │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 info`

```
Usage: t3 info [OPTIONS]

 Show t3 installation, teatree/overlay sources, and editable status.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 dashboard`

```
Usage: t3 dashboard [OPTIONS]

 Migrate the database and start the dashboard dev server.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --host           TEXT     Host to bind to [default: 127.0.0.1]               │
│ --port           INTEGER  Port to serve on [default: 8000]                   │
│ --project        PATH     Project root to serve from (worktree path).        │
│ --workers        INTEGER  Number of background task workers to start (0 to   │
│                           disable)                                           │
│                           [default: 1]                                       │
│ --stop                    Stop the running dashboard and exit.               │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 config`

```
Usage: t3 config [OPTIONS] COMMAND [ARGS]...

 Configuration and autoloading.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ check-update       Check if a newer version of teatree is available.         │
│ write-skill-cache  Write overlay skill metadata to XDG cache for hook        │
│                    consumption.                                              │
│ autoload           List skill auto-loading rules from context-match.yml      │
│                    files.                                                    │
│ cache              Show the XDG skill-metadata cache content.                │
│ deps               Show resolved dependency chain for a skill.               │
│ test-trigger       Test which skill would be triggered for a given prompt.   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config check-update`

```
Usage: t3 config check-update [OPTIONS]

 Check if a newer version of teatree is available.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config write-skill-cache`

```
Usage: t3 config write-skill-cache [OPTIONS]

 Write overlay skill metadata to XDG cache for hook consumption.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config autoload`

```
Usage: t3 config autoload [OPTIONS]

 List skill auto-loading rules from context-match.yml files.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config cache`

```
Usage: t3 config cache [OPTIONS]

 Show the XDG skill-metadata cache content.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config deps`

```
Usage: t3 config deps [OPTIONS] SKILL

 Show resolved dependency chain for a skill.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    skill      TEXT  [required]                                             │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config test-trigger`

```
Usage: t3 config test-trigger [OPTIONS] PROMPT

 Test which skill would be triggered for a given prompt.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    prompt      TEXT  [required]                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 ci`

```
Usage: t3 ci [OPTIONS] COMMAND [ARGS]...

 CI pipeline helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ cancel              Cancel stale CI pipelines for a branch.                  │
│ divergence          Check fork divergence from upstream.                     │
│ fetch-errors        Fetch error logs from the latest CI pipeline.            │
│ fetch-failed-tests  Extract failed test IDs from the latest CI pipeline.     │
│ trigger-e2e         Trigger E2E tests on CI.                                 │
│ quality-check       Run quality analysis (fetch test report from latest      │
│                     pipeline).                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci cancel`

```
Usage: t3 ci cancel [OPTIONS] [BRANCH]

 Cancel stale CI pipelines for a branch.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci divergence`

```
Usage: t3 ci divergence [OPTIONS]

 Check fork divergence from upstream.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci fetch-errors`

```
Usage: t3 ci fetch-errors [OPTIONS] [BRANCH]

 Fetch error logs from the latest CI pipeline.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci fetch-failed-tests`

```
Usage: t3 ci fetch-failed-tests [OPTIONS] [BRANCH]

 Extract failed test IDs from the latest CI pipeline.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci trigger-e2e`

```
Usage: t3 ci trigger-e2e [OPTIONS] [BRANCH]

 Trigger E2E tests on CI.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci quality-check`

```
Usage: t3 ci quality-check [OPTIONS] [BRANCH]

 Run quality analysis (fetch test report from latest pipeline).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 review`

```
Usage: t3 review [OPTIONS] COMMAND [ARGS]...

 Code review helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ post-draft-note      Post a draft note on a GitLab MR (inline or general).   │
│ delete-draft-note    Delete a draft note from a GitLab MR.                   │
│ publish-draft-notes  Publish all draft notes on a GitLab MR (bulk submit).   │
│ list-draft-notes     List draft notes on a GitLab MR.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review post-draft-note`

```
Usage: t3 review post-draft-note [OPTIONS] REPO MR NOTE

 Post a draft note on a GitLab MR (inline or general).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
│ *    note      TEXT     Comment text (markdown) [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --file        TEXT     File path for inline comment (omit for general note)  │
│ --line        INTEGER  Line number in the new file (must be an added line)   │
│                        [default: 0]                                          │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review delete-draft-note`

```
Usage: t3 review delete-draft-note [OPTIONS] REPO MR NOTE_ID

 Delete a draft note from a GitLab MR.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo         TEXT     GitLab project path [required]                    │
│ *    mr           INTEGER  Merge request IID [required]                      │
│ *    note_id      INTEGER  Draft note ID to delete [required]                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review publish-draft-notes`

```
Usage: t3 review publish-draft-notes [OPTIONS] REPO MR

 Publish all draft notes on a GitLab MR (bulk submit).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review list-draft-notes`

```
Usage: t3 review list-draft-notes [OPTIONS] REPO MR

 List draft notes on a GitLab MR.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path [required]                       │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 review-request`

```
Usage: t3 review-request [OPTIONS] COMMAND [ARGS]...

 Batch review requests.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ discover  Discover open merge requests awaiting review.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review-request discover`

```
Usage: t3 review-request discover [OPTIONS]

 Discover open merge requests awaiting review.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 doctor`

```
Usage: t3 doctor [OPTIONS] COMMAND [ARGS]...

 Smoke-test hooks, imports, services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ check  Verify imports, required tools, and editable-install sanity.          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 doctor check`

```
Usage: t3 doctor check [OPTIONS]

 Verify imports, required tools, and editable-install sanity.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 tool`

```
Usage: t3 tool [OPTIONS] COMMAND [ARGS]...

 Standalone utilities.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ privacy-scan     Scan text for privacy-sensitive patterns (emails, keys,     │
│                  IPs).                                                       │
│ analyze-video    Decompose video into frames for AI analysis.                │
│ bump-deps        Bump pyproject.toml dependencies from uv.lock.              │
│ sonar-check      Run local SonarQube analysis via Docker.                    │
│ claude-handover  Show Claude handover telemetry and runtime recommendations. │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool privacy-scan`

```
Usage: t3 tool privacy-scan [OPTIONS] [PATH]

 Scan text for privacy-sensitive patterns (emails, keys, IPs).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   path      [PATH]  File or '-' for stdin [default: -]                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool analyze-video`

```
Usage: t3 tool analyze-video [OPTIONS] VIDEO_PATH

 Decompose video into frames for AI analysis.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    video_path      TEXT  Path to video file [required]                     │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool bump-deps`

```
Usage: t3 tool bump-deps [OPTIONS]

 Bump pyproject.toml dependencies from uv.lock.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool sonar-check`

```
Usage: t3 tool sonar-check [OPTIONS] [REPO_PATH]

 Run local SonarQube analysis via Docker.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   repo_path      [REPO_PATH]  Path to repo (default: current directory)      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --skip-baseline    --no-skip-baseline      Reuse previous baseline           │
│                                            [default: no-skip-baseline]       │
│ --remote           --no-remote             Push to CI server instead of      │
│                                            local                             │
│                                            [default: no-remote]              │
│ --remote-status    --no-remote-status      Fetch CI Sonar results            │
│                                            [default: no-remote-status]       │
│ --help                                     Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool claude-handover`

```
Usage: t3 tool claude-handover [OPTIONS]

 Show Claude handover telemetry and runtime recommendations.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --current-runtime        TEXT  Current CLI runtime. Defaults to the          │
│                                highest-priority configured runtime.          │
│ --session-id             TEXT  Claude session ID to inspect. Defaults to     │
│                                latest telemetry.                             │
│ --state-dir              PATH  Override the Claude statusline telemetry      │
│                                directory.                                    │
│ --json                         Emit machine-readable JSON.                   │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 setup`

```
Usage: t3 setup [OPTIONS] COMMAND [ARGS]...

 First-time setup and global skill management.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claude-scope        TEXT  Claude plugin install scope: user or project.    │
│                             [default: user]                                  │
│ --skip-plugin               Skip Claude CLI plugin registration.             │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 assess`

```
Usage: t3 assess [OPTIONS] COMMAND [ARGS]...

 Codebase health assessment.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ run      Run deterministic codebase metrics on a repository.                 │
│ history  Show assessment history for a repository.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 assess run`

```
Usage: t3 assess run [OPTIONS]

 Run deterministic codebase metrics on a repository.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --root                 PATH  Repository root to assess                       │
│ --json                       Output raw JSON                                 │
│ --save    --no-save          Save results to .t3/assessments/                │
│                              [default: save]                                 │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 assess history`

```
Usage: t3 assess history [OPTIONS]

 Show assessment history for a repository.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --root           PATH     Repository root                                    │
│ --limit  -n      INTEGER  Number of recent assessments to show [default: 10] │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 infra`

```
Usage: t3 infra [OPTIONS] COMMAND [ARGS]...

 Teatree-wide infrastructure services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ redis  Shared Redis container (teatree-redis).                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 infra redis`

```
Usage: t3 infra redis [OPTIONS] COMMAND [ARGS]...

 Shared Redis container (teatree-redis).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ up      Start the shared Redis container (idempotent).                       │
│ down    Stop the shared Redis container.                                     │
│ status  Print the shared Redis container status.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis up`

```
Usage: t3 infra redis up [OPTIONS]

 Start the shared Redis container (idempotent).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis down`

```
Usage: t3 infra redis down [OPTIONS]

 Stop the shared Redis container.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis status`

```
Usage: t3 infra redis status [OPTIONS]

 Print the shared Redis container status.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```
