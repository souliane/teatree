# Admin dashboard snapshot

A live "screenshot" of the teatree admin dashboard — the Django admin index listing
every registered `teatree.core` domain model. It is **generated**, not hand-authored:
`scripts/hooks/generate_dashboard_snapshot.py` renders the page through Django's test
client and writes [`admin-index.html`](generated/dashboard/admin-index.html). CI
regenerates it and fails on drift (`git diff --exit-code docs/generated`), so
registering a new admin model updates this snapshot automatically. Edit
`src/teatree/core/admin.py`, not the HTML.

`/admin/` is re-skinned to match `/dash/`: a `templates/admin/base_site.html`
override links the shared design tokens (`dash/css/tokens.css`) and an admin-var
mapping (`dash/css/admin-theme.css`), so the snapshot carries those two
stylesheet links. The generator hook re-fires when either the override or the
theme CSS changes.

<iframe src="../generated/dashboard/admin-index.html" title="teatree admin dashboard snapshot"
        style="width: 100%; height: 640px; border: 1px solid var(--md-default-fg-color--lightest);"></iframe>

[Open the full-page snapshot](generated/dashboard/admin-index.html)

## Skills control plane

`/dash/skills/` answers three different questions without conflating them:

1. **What TeaTree loads into prompts.** The read-only TeaTree table expands every
   `skills/*/SKILL.md` declaration, its direct and transitive `requires`, the
   agents that declare it directly or as a companion, and the agent/phase prompt
   closures in which it is actually embedded. Missing references and dependency
   cycles are reported below the table.
2. **What TeaTree independently requires.** Third-party skills named by
   `apm.yml`, unresolved skill references, and active overlay runtime demands are
   listed as read-only requirements. `apm.yml` remains the source declaration;
   the dashboard intentionally does not edit it.
3. **What each interactive harness has installed.** Claude Code and Codex get
   separate inventories. Each row shows the skill-manager source, the harness
   front door, whether that front door is a copy, symlink, broken link, or
   plugin-owned path, and any cached git provenance.

The normal `GET` path is deliberately pure: it reads TeaTree declarations, DB
configuration, and the last inventory receipt, but starts no subprocess and
makes no network request. **Refresh inventory** is an explicit authenticated
`POST`. It runs the installed, exactly pinned `skills` CLI and, for linked git
checkouts, fetches the origin default branch once per repository. The resulting
receipt records the local branch (or detached state), full local and default
branch SHAs, ahead/behind counts, and whether a newer default-branch revision is
available. Failures and timeouts produce an `Unknown` diagnostic rather than a
plausible stale answer.

Optional harness skills can be removed from their row. Removal is another
authenticated `POST`, requires confirmation, checks the exact CLI version before
mutation, removes only that harness installation, and persists a
`<harness>:<skill>` exclusion so a later headless `t3 setup` does not reinstall
it. A skill stays read-only when TeaTree has an independent requirement for it;
TeaTree's own prompt declarations are always read-only.

A harness-installed skill whose manifest name equals a first-party `t3` skill is
a collision even when the qualified namespaces differ. Both rows and the Skills
navigation badge turn red because agents can still select the wrong prose. The
harness duplicate remains removable unless it is independently required by the
external-demand rules above.
