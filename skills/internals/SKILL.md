---
name: internals
description: "How teatree is BUILT and how to change it safely — architecture, lifecycle phases, key models, the overlay API, the `t3` CLI reference, and the management-command rules whose violation fails SILENTLY (a `typer.Exit` under `call_command` exits 0, so CI reports green on a real failure). Load it when writing or reviewing teatree's own code, or when building an overlay on it. Carries no Claude Code harness wiring — that is `/t3:interactive` — and no dogfooding procedure — that is `/t3:dogfooding`."
eval_exempt: reference for teatree's own internals; the behaviours it describes are graded by the code/review skills' evals and by the repo's own gates, not by a trajectory over this overview
compatibility: macOS/Linux, a teatree checkout; reading-only, no services required.
metadata:
  version: 0.0.1
---

# TeaTree — Internals

TeaTree is a personal code factory for multi-repo projects — it turns a ticket URL into a merged pull request by driving AI agents through lifecycle phases. Under the hood it's a Django project; overlays are lightweight Python packages that extend it for specific projects.

## Architecture

- **TeaTree IS the Django project.** Requires a local clone; installed via `uv tool install --editable . --overrides uv-overrides.txt`
  (the flag is required — `uv tool install` does not read `[tool.uv] override-dependencies`).
- **Overlays** register via `teatree.overlays` entry points and provide project-specific configuration. <!-- skill-symbol-ref: entry-point group name, not an importable module -->
- **Skills** live in `skills/` and are loaded by the agent's skill system.
- **Hooks** in `hooks/scripts/` run on agent lifecycle events (e.g., prompt submit, pre/post tool use).

## Lifecycle Phases

`CANONICAL_PHASES` (`teatree.core.modelkit.phases`) holds ten, not the six a shorter drawing
suggests — the four extra ones are where most of the gating actually happens:

```
scoping → planning → coding → testing → e2e → e2e_reviewing → reviewing → requesting_review → shipping → retro
```

Each phase maps to a skill (`t3:ticket`, `t3:code`, …). The `Session` model tracks visited
phases and enforces quality gates (e.g. can't ship without testing).

**Posture: autonomous end-to-end completion of in-scope tickets.** The resolved default is that the factory carries an in-scope ticket all the way through these phases without pausing to ask "should I continue?". When a ticket sits at a phase boundary (e.g. `TESTED`) with no blocker and no genuine decision, the agent's next action *advances* it toward ship/review — it does not stall on a permission check the user never needs to answer (cross-ref `/t3:rules` § "Publishing Actions Are Mode-Conditional" for `auto` vs `interactive`, and the autonomy posture in CLAUDE.md). A pause is reserved for a real blocker or a genuine ask (a debatable architectural choice, an ambiguous destination); the absence of those is the signal to proceed, not to check in. Pinned by `evals/scenarios/factory_finishes_in_scope_ticket.yaml`.

## CLI Reference

Top-level commands (no overlay needed): `t3 startoverlay`, `t3 docs`, `t3 agent`, `t3 sessions`, `t3 cost`, `t3 tokens`, `t3 speak`, `t3 ui`, `t3 admin`, `t3 info`, `t3 config`, `t3 banned-terms`, `t3 ci`, `t3 codex`, `t3 review`, `t3 review-request`, `t3 eval`, `t3 doctor`, `t3 tool`, `t3 setup`, `t3 update`, `t3 assess`, `t3 overlay`, `t3 loop`, `t3 mcp`, `t3 notion`, `t3 slack`, `t3 task`, `t3 recover`, `t3 dogfood`, `t3 dream`, `t3 mutation`, `t3 push`. (This list is kept honest by `tests/teatree_skill_support/test_teatree_skill_cli_reference.py`, which asserts every name is a registered `t3` command; the in-sync full reference with descriptions is `docs/generated/cli-reference.md`.)

Overlay-scoped commands require `t3 <overlay> <subcommand>` (e.g., `t3 teatree`):

```bash
t3 loop start                         # Spawn the loop-owner session (registers each enabled loop's /loop)
t3 loops tick --loop <name>           # Run one enabled loop's tick (per-loop only; bare `t3 loops tick` is a hard error, #2650)
t3 loop status                        # Show the loop's last-rendered statusline
t3 <overlay> resetdb                  # Drop and recreate the SQLite database
t3 <overlay> worktree provision          # Provision worktree (ports, DB, overlay steps)
t3 <overlay> worktree start          # Start dev servers
t3 <overlay> worktree status         # Show worktree state
t3 <overlay> worktree teardown       # Stop services, clean up
t3 <overlay> tasks work-next          # Claim and execute the next pending task
t3 <overlay> pr create <ticket-id>    # Open the PR: validate ship gates + trigger the ship transition (advance a TESTED ticket toward review)
t3 <overlay> followup sync            # Daily ticket/PR sync
```

### Notion, headless (`t3 notion`)

The claude.ai Notion connector is interactively authenticated, so it does not exist in a
cron/headless run. `t3 notion` is the replacement: the public Notion API under an internal
**integration token** (env `NOTION_TOKEN`, else the overlay's `NOTION_TOKEN_PASS_KEY` entry,
else `pass show notion/integration-token`). Agents call `t3`, never the API directly.

```bash
t3 notion whoami                       # verify the token; print the integration pages must be shared with
t3 notion doctor <page>                # triage one page: token present? valid? shared? still LIVE?
t3 notion fetch <page> --comments      # page as Markdown, plus its open discussions (--json for raw blocks)
t3 notion audit-fetch <page> --reason '<why>'     # audit-read a page refused as dead; stamps the output
t3 notion append <page> --body-file f  # append at the end, verified by a re-fetch
t3 notion section show <page> --heading '## 🔧 …'      # which blocks the owned section covers
t3 notion section replace <page> --heading '## 🔧 …' --body-file f --legacy '## 🔧 …old…'
t3 notion comment post <page> --body-file f --marker '[t3:…]'   # post once per marker; re-post needs --allow-duplicate
t3 notion property get <page> --name 'GitLab Reference'         # the poll a block fetch cannot answer (--json for raw)
t3 notion property set <page> --name Status --value Merged      # payload shaped by the property's OWN live type
t3 notion query <database-id> [--data-source] [--filter-file f]
```

**One-time human setup** (no code can do it): create an internal integration, store its
secret in `pass`, and **share the integration onto each page/database** (page ••• →
Connections). Until that grant exists Notion answers 404 — reported as "not shared with this
integration", distinct from "not a Notion object".

**`section replace` is the idempotency primitive** the `/prd-agent` and `/bdd-test-creation`
skills own a named H2 section with. It is **block-scoped**: it appends the new body under the
matched heading, archives only that section's own blocks, and renames a legacy heading in
place — so block-level comments and discussions outside (and on the heading itself) survive.
There is no whole-page replace on this surface, because one would destroy every discussion on
the page. Two matching headings stop the write rather than guessing.

**`comment post` is dedup-driven by default.** The same skills post a notification comment and
check for their own marker first, so the marker already being in the page's open discussions
reports `duplicate` at exit 0 having written nothing — a forgotten flag under-posts, never
double-posts, and `--allow-duplicate` is the deliberate second copy. Notion exposes only
*unresolved* discussions, so a marker whose thread a human resolved is posted again.

**`property get`/`set` reach what `fetch` cannot.** A property hangs off the page object rather
than its blocks, so a block-tree render can never answer "what is this page's GitLab Reference?".
The write derives its payload from the property's own live type — the caller names a value, never
a Notion type — and a type with no plain-text form (formula, rollup, relation, people, files) is
refused rather than written as something else.

**An archived page is refused, not returned.** An archived Notion page renders as COMPLETELY
current — full acceptance criteria, a `Status` still reading "In Progress", live comment
threads — so a superseded spec is read as the requirement and the work built against it is
the WRONG work. Every page-scoped command goes through one liveness chokepoint and exits
`14` rather than handing back the body. The predicate: `archived`/`in_trash` is the only
signal that CONCLUDES death and is decisive; membership of the page's own parent database is
the corroboration that turns an unverifiable read into an explicit UNKNOWN and supplies the
successor. **UNKNOWN refuses** — a parent database this integration cannot query fails closed.
`t3 notion doctor` reports liveness as its own third stage (`OK`/`DEAD`/`UNKNOWN`, never a
bare OK it did not establish). The only escape is a READ escape for a genuine audit —
`t3 notion audit-fetch <page> --reason '<why>'`, its own command rather than a flag on
`fetch` so it cannot be reached by habit. It needs written prose, announces itself on stderr,
and stamps the emitted Markdown; there is no audited write.

Every write re-fetches and refuses to report success unless the change landed. Failures exit
with their own codes so an unattended caller can branch: `3` no token, `4` bad token,
`5` capability not granted, `6` page not shared, `7` not a Notion object, `8` rate-limited,
`9` write reported but not landed, `10` ambiguous section, `11` unrepresentable Markdown,
`14` page archived/superseded or its liveness unprovable.

## Key Models

- **Ticket** — issue URL, overlay, variant, repos
- **Worktree** — repo path, branch, ports, state (FSM: created → provisioned → services_up → ready)
- **Session** — agent session with visited phases, repos modified/tested
- **Task** — claimable work unit with lease, heartbeat, parent chain
- **TaskAttempt** — execution result with exit code, structured output

These models are surfaced in a small Django admin dashboard. A rendered, drift-checked HTML snapshot of it — generated through Django's test client by `scripts/hooks/generate_dashboard_snapshot.py` — lives at [docs/generated/dashboard/admin-index.html](../../docs/generated/dashboard/admin-index.html) as an always-fresh "screenshot". The CLI front door gets the same treatment: the deterministically-rendered output of `t3 --help` + `t3 loop --help` (via `scripts/hooks/generate_cli_output_snapshot.py`) is captured at [docs/generated/cli/representative-output.md](../../docs/generated/cli/representative-output.md), the curated complement to the exhaustive CLI reference.

## Overlay API

The API is **faceted**. `OverlayBase` keeps only the handful of hooks that are not about one
subsystem; everything else lives on a composed facet reached as `overlay.<facet>.<method>`.
A flat `get_*` name on the base is the pre-facet shape — it no longer resolves, and an
override written against it is silently dead code the caller never invokes.

Still on `OverlayBase`:

- `get_repos()` — repo list for worktree creation
- `get_provision_steps(worktree)` — setup steps (migrations, fixtures)
- `get_workspace_repos()` — repos the workspace commands span
- `get_statusline_segments()`, `get_issue_title(url)`,
  `is_issue_done(url)`, `resolve_mr_token(...)`, `resolve_issue_token(...)`,
  `get_timeouts()`, `get_health_signals()`, `get_checking_sources()`,
  `get_eval_scenarios_dir()`

On the facets (`overlay.provisioning`, `.runtime`, `.e2e`, `.review`, `.config`,
`.connectors`, `.metadata`):

| Facet | Methods |
|---|---|
| `provisioning` | `env_extra(worktree)`, `db_import_strategy(worktree)`, `db_import(...)`, `post_db_steps(...)`, `services_config(worktree)`, `compose_file(...)`, `symlinks(...)`, `envrc_lines(...)`, `docker_services(...)`, `health_checks(...)`, `cleanup_steps(...)`, `resolve_variant(...)` |
| `runtime` | `run_commands(worktree)`, `pre_run_steps(...)`, `test_command(...)`, `lint_command(...)`, `verify_endpoints(...)`, `readiness_probes(...)` |
| `e2e` | `env_extras(...)`, `preflight(...)`, `run_provenance(spec_path)`, `scenarios(spec_path)`, `playwright_args(spec_path)`, `spec_paths(...)` |
| `review` | `visual_qa_targets(changed_files)`, `can_auto_merge(...)`, `merge_candidate_repo_slugs(...)`, `review_exempt_repo_slugs(...)` |
| `config` | `get_gitlab_token()`, `get_github_token()`, `get_slack_token()`, `get_review_channel()`, `secret_pass_key(...)`, … (credentials, URLs, labels) |
| `connectors` | `preflight(...)`, `mcp_provider_expectations()`, `manifest()` |

There is no `get_gitlab_url()` anywhere: the URL is a pydantic field on `OverlayConfig`, not a
method. Reaching for one is the reliable sign a doc predates the facet split.

## Management Command Patterns

Teatree's CLI groups (`t3 <overlay> <group> <sub>`) are django-typer `TyperCommand` classes invoked via Django's `call_command` (see `src/teatree/cli/overlay.py:430` → `managepy(...)`). To propagate a non-zero exit code from a subcommand, **use `raise SystemExit(N)` — NOT `raise typer.Exit(code=N)`**.

`typer.Exit` is designed for the typer CLI runner; when it's raised inside a TyperCommand reached via `call_command`, the exception is silently swallowed and the process exits 0 even though the failure was raised. `SystemExit` bubbles up through Django management → `subprocess.run(check=True)` → CLI exit code.

- Canonical example: `src/teatree/core/management/commands/tasks.py:19` — `raise SystemExit(1)` after `self.stderr.write(...)`.
- Tests: `with pytest.raises(SystemExit) as exc_info: call_command(...)` then assert `exc_info.value.code == N`. `pytest.raises(typer.Exit)` reports `DID NOT RAISE` even though the source did raise — call_command eats it before pytest sees it.
- `typer.Exit` is still correct in `src/teatree/cli/*.py` files that go through the typer runner directly (different call site).
- Anti-pattern: returning an error string from a management command instead of raising. The CLI exits 0 and CI reports green on real failures.

### Structured refusals: return the dict, inherit the non-zero exit

A refusal that an in-process caller must route on (the `mcp` write tools, the loop) is RETURNED as `{"error": …, "hint": …}`, not raised — raising would destroy the value those callers read. That is the one sanctioned form of the anti-pattern above, and it is only sanctioned because the exit code is restored at the boundary: inherit `teatree.core.management.refusal_exit.RefusalExitTyperCommand` instead of `TyperCommand`, and a returned refusal exits `1` on the argv path while `call_command` still gets the dict verbatim.

- The predicate is one pure function, `refusal_exit_code(result)` — non-zero iff the result is a mapping with a truthy `error`. A new refusal shape therefore needs an `error` key and nothing else; a shape without one silently exits 0 again.
- The gate is `_called_from_command_line`, the flag Django's `run_from_argv` sets and `call_command` does not — so the shell and the in-process consumer get opposite, correct answers from one refusal.
- Loud is the default; `soft_refusal_commands` exempts named subcommands, so a *new* refusal is loud without being listed anywhere. Exempt one only when its caller depends on a *soft* refusal: `pr ensure-pr` is the pre-push hook's entry point, where reporting and letting the push through is the designed behaviour (#792).
- Seven groups carry it via this base class: `pr`, `ticket`, `review`, `repro`, `lifecycle`, `e2e`, `followup`. `ticket` is the sharpest — `t3 <overlay> ship <id> && t3 <overlay> ticket clear …` now stops on a refused ship, and `ticket merge` still hands `CallCommandMergeKeystone.merge_clear` the five keys it routes on. Canonical example: `src/teatree/core/management/commands/pr.py` `Command` — its control-DB, missing-ticket, missing-worktree and ship-gate refusals all exit non-zero.
- Give the `Command` a one-line class docstring. Without one, `docs/generated/management-commands.*` and `--help` inherit the *base class's* docstring and advertise the seam's internals as the command group's description.
- A command that has no in-process consumer of its failure still uses `raise SystemExit(N)` — that is simpler and stays the default.
- The guard is `tests/teatree_core/management_commands/test_exit_contract_seam.py`: an AST ratchet refuses a `{"error": …}` return in a class that does not inherit the seam, and a non-zero int return anywhere in the command tree — the `ast.Constant`, `ast.IfExp`, `ast.UnaryOp` (`return -1`) and `ast.BoolOp` (`return failures and 1 or 0`) shapes — plus a live `run_from_argv` case per refusing subcommand. Static AST scanning cannot see a computed/variable return, so this is a strong low-false-positive backstop, not a proof the anti-pattern can never recur — the tree is clean *as far as that ratchet reads*, which is the only claim to make about it; its constant-only first cut scored `return 0 if ok else 1` as clean and let an `env` site through, so widen the detector before reading a green as coverage of a new return shape.
- The ratchet only reads a literal `{"error": …}` at the `@command`-decorated method's own `return`. A refusal computed in a private helper and handed back through a `Call` — `return json.dumps(self._run(...))`, `return self._helper(...)` — is invisible to it in both directions (dict-shaped *or* wrapped-to-`str`), which is exactly how `retro review-findings` escaped both this guard and the runtime seam (a cold review of #4235 found it: `_run`'s five `{"error": …}` sites reach the CLI through a `json.dumps` that turns the return into a `str`, so `refusal_exit_code`'s `isinstance(result, Mapping)` check never fires either). Fixed there by routing `refusal_exit_code` by hand at the one call site rather than widening the shared ratchet — a broader helper-method walk also flags `handover.py`'s list-nested `error` key and `tasks.py`'s `routing_error` substring match, both already-accepted non-escapes, so it is not a safe default to reach for. A new command that wraps its own refusal the same way needs the same local, hand-routed check; the base class alone does not catch it. **Write the payload before you raise.** The base class raises from `execute()`, *after* `super().execute()` has already printed the result, so its refusals reach the shell with the `error` payload on stdout. A hand-routed check raises from inside the command method, which lands before any write — a first cut of the `retro` fix exited 1 with both streams empty, which is worse than the exit 0 it replaced: the operator gets a failure with no reason and every machine consumer loses the payload. `t3 <overlay> retro review-findings` is the reference shape (`self.stdout.write(json.dumps(result))`, then `raise SystemExit(code)`).

### Annotated typer options must have defaults for `call_command`

`Annotated[str, typer.Option(help="...")]` parameters without a default value make the command unusable via Django's `call_command` — it raises `Missing parameter: <name>` even when the caller passes the kwarg. Give every `typer.Option`-annotated parameter a default (e.g. `= ""`) and validate at runtime (`if not phase.strip(): raise SystemExit(1)`). This keeps both CLI and `call_command` call sites happy.

Canonical example: `src/teatree/core/management/commands/tasks.py` `create` subcommand — `phase: Annotated[str, typer.Option(...)] = ""` + runtime non-blank check.

## Configuration

Environment variables read by hooks:

```bash
T3_REPO="$HOME/workspace/<your-username>/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # gate pushes behind an explicit prompt
T3_AUTO_PUSH_FORK=false                      # auto-push to fork when T3_PUSH=true and origin ≠ T3_UPSTREAM
T3_MODE=interactive                          # auto = push without waiting for approval; interactive (default) gates push on user approval. DB-home equivalent: config_setting set mode auto. (Supersedes the retired T3_AUTO_SHIP, #2697)
T3_PRIVACY=strict                            # block commits with PII
```

## Directive Loop: the Ratified Sketch is Byte-Law (Non-Negotiable)

A directive's activation is applied by exactly one actor — the directive loop's CONFIGURING step — and only ever **byte-identical** to the ratified `MechanismSketch` (key, value, and scope). The human ratified a specific design; an operator or agent that hand-runs a *differing* `config_setting set` for a directive-governed key has silently overruled that ratification, and the loop's own drift guard (`activation_conforms`) refuses the drifted write anyway. When a polluted context nudges toward a value that differs from the ratified sketch — "just set 2, basically the same" — do X, never Y:

- **Do** leave the byte-identical write to the loop's CONFIGURING step; or route an amendment through re-interpret → re-ratify (a NEW generation via `t3 directive …`); or surface the discrepancy with a structured `AskUserQuestion`.
- **Never** hand-run `t3 <overlay> config_setting set <directive-key> <drifted-value>` to apply a value that differs from the ratified sketch. A "basically the same" value is a different design and needs a fresh ratification, not a hand-edit.

```bash
# ratified sketch: max_open_prs_per_repo_per_ticket = 1. do X — amend via re-ratify, never hand-apply a drifted value:
t3 directive history          # inspect the ratified activation; an amendment re-interprets → re-ratifies (generation+1)
# never Y — hand-applying a value that differs from the ratified sketch:
# t3 <overlay> config_setting set max_open_prs_per_repo_per_ticket 2   # FORBIDDEN — drift from the ratified sketch
```
