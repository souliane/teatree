# BLUEPRINT Appendix — Overlay System

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §6. Consumer cross-references such as `BLUEPRINT §6` resolve here.

## 6. Overlay System

An overlay is a downstream Django project that customizes teatree for a specific project/organization.

### 6.0 Overlay Thinness Principle (Non-Negotiable)

**Overlays must be as thin as possible.** Generic workflow logic belongs in teatree core, not in overlays.

Before adding logic to an overlay, ask: "Would a different project using the same framework (Django, Node, etc.) need the same logic?" If yes, it belongs in core — parameterized and configurable. The overlay should only provide:

1. **Configuration values** — repo names, env vars, credentials, file paths, naming conventions
2. **Project-specific glue** — connecting to a proprietary API, custom tenant detection, product-specific feature flags
3. **Truly unique workflows** — steps that no other project would ever need

Everything else — DB provisioning strategies, migration runners, symlink management, service orchestration, dump fallback chains — must be implemented as configurable engines in core. The overlay configures the engine; the overlay does not reimplement the engine.

**Why this matters:** When logic lives in an overlay, it is tested only by that overlay's test suite, invisible to other overlays, and duplicated when a second project needs the same workflow. Core code has >90% branch coverage, is reviewed against the BLUEPRINT, and benefits all overlays.

**Refactoring signal:** If an overlay method exceeds ~30 lines of non-configuration code, it likely contains generic logic that should be extracted to core.

### 6.1 OverlayBase ABC

Defined in `teatree.core.overlay`. All methods receive the `worktree` instance for context.

**Overlay API version pin.** ``teatree.__overlay_api_version__`` (currently ``"1"``) is bumped on any **breaking** change to the overlay-facing API: ``OverlayBase`` method signatures, ``Worktree``/``Ticket`` fields overlays read, the ``teatree.overlays`` entry-point contract, or runner protocols overlays may implement. Overlays assert this at import to fail loudly when teatree diverges from what they were built against — no silent misbehavior, no shim, no deprecation warning. Non-breaking additions (new optional hook, new helper) leave the version alone.

**Abstract methods (must implement):**

| Method | Signature | Purpose |
|--------|-----------|---------|
| `get_repos()` | `→ list[str]` | Declare repositories for provisioning |
| `get_provision_steps(worktree)` | `→ list[ProvisionStep]` | Ordered setup steps |

**Optional methods (override as needed):**

| Method | Signature | Default | Purpose |
|--------|-----------|---------|---------|
| `get_env_extra(worktree)` | `→ dict[str, str]` | `{}` | Extra environment variables |
| `declared_secret_env_keys()` | `→ set[str]` | `set()` | Keys whose values must NOT land in `.t3-env.cache` (still produced by `get_env_extra` so subprocess `env=` callers receive them, but `render_env_cache` filters them out of the on-disk file). Core auto-includes `POSTGRES_PASSWORD` and writes a `POSTGRES_PASSWORD_PASS_KEY` reference instead — runtime callers resolve the literal via `teatree.utils.postgres_secret.resolve_postgres_password`. Use this hook for additional `pass`-sourced credentials. |
| `uses_redis()` | `→ bool` | `False` | Whether the shared `teatree-redis` container should be ensured and a per-ticket DB index allocated. Multi-service overlays with Celery/RQ/cache opt in; single-service overlays leave the default. |
| `get_run_commands(worktree)` | `→ dict[str, str]` | `{}` | Named service run commands |
| `get_test_command(worktree)` | `→ str` | `""` | Test suite command |
| `get_db_import_strategy(worktree)` | `→ DbImportStrategy \| None` | `None` | DB provisioning strategy |
| `get_post_db_steps(worktree)` | `→ list[PostDbStep]` | `[]` | Post-DB-setup callbacks |
| `get_reset_passwords_command(worktree)` | `→ str` | `""` | Dev password reset command |
| `get_symlinks(worktree)` | `→ list[SymlinkSpec]` | `[]` | Extra symlinks |
| `get_services_config(worktree)` | `→ dict[str, ServiceSpec]` | `{}` | Service metadata |
| `get_base_images(worktree)` | `→ list[BaseImageConfig]` | `[]` | Docker base images teatree builds once and shares across worktrees |
| `get_docker_services(worktree)` | `→ set[str]` | `set()` | Service names (keys of `get_services_config`) that MUST run in Docker |
| `validate_pr(title, description)` | `→ ValidationResult` | no errors | PR validation rules |
| `get_followup_repos()` | `→ list[str]` | `[]` | GitLab project paths to sync |
| `get_skill_metadata()` | `→ SkillMetadata` | `{}` | Active skill path + companions |
| `get_ci_project_path()` | `→ str` | `""` | GitLab project path for CI |
| `get_e2e_config()` | `→ dict[str, str]` | `{}` | E2E trigger config |
| `detect_variant()` | `→ str` | `""` | Tenant detection |
| `get_workspace_repos()` | `→ list[str]` | `get_repos()` | Repos for workspace ticket creation |
| `get_tool_commands()` | `→ list[ToolCommand]` | `[]` | Overlay-specific CLI tools |
| `get_visual_qa_targets(changed_files)` | `→ list[str]` | `[]` | URL paths the pre-push browser sanity gate should load |
| `get_e2e_env_extras(env_cache)` | `→ dict[str, str]` | `{}` | Overlay-specific env vars merged into the Playwright environment (e.g. `WT_VARIANT`→`CUSTOMER`) |
| `get_e2e_preflight(customer, base_url)` | `→ list[Callable[[], None]]` | `[]` | Pre-Playwright gates; each callable raises `RuntimeError` on failure |

**Auto-close trailer rejection (`OverlayConfig.forbid_close_keywords`, default `False`, [#1012](https://github.com/souliane/teatree/issues/1012)).** An overlay that manages issue closure through the forge's linked-items API — not `Closes/Fixes/Resolves #N` auto-close trailers — sets `config.forbid_close_keywords = True`. The `_run_ship_gates` sequence then runs `_close_keyword_gate.run_close_keyword_gate` after the shipping gate and before visual QA: it scans the **raw** MR-description source (the branch's last commit message, pre-`sanitize_close_keywords` — that rewrite would otherwise mask the trailer) and every branch commit body for the full GitHub/GitLab auto-close keyword set (`close/closes/closed`, `fix/fixes/fixed`, `resolve/resolves/resolved`; `#N`, `<project>#N`, full-URL forms; case-insensitive) and `raise SystemExit` with the offending line + a suggested `Relates to` rewrite. A merged trailer would otherwise auto-close the referenced issue and break the lifecycle FSM. teatree's own overlay leaves the flag at its `False` default, so teatree PRs that legitimately use `Closes #N` are unaffected — the gate is a no-op unless an overlay opts in.

### 6.2 Supporting TypedDicts

```python
ProvisionStep(name: str, callable: Callable[[], None], required: bool = True, description: str = "")
PostDbStep(name: str, description: str, command: str)           # all total=False
SymlinkSpec(path: str, source: str, mode: str, description: str)
ServiceSpec(shared: bool, service: str, compose_file: str, start_command: str, readiness_check: str, base_image: str)
BaseImageConfig(image_name: str, dockerfile: str, lockfile: str, build_context: Path, env_var: str, build_args: dict[str, str])
DbImportStrategy(kind: str, source_database: str, shared_postgres: bool, snapshot_tool: str, restore_order: list[str], notes: list[str], worktree_repo_path: str)
SkillMetadata(skill_path: str, companion_skills: list[str])
ValidationResult(errors: list[str], warnings: list[str])       # total=True
ToolCommand(name: str, help: str, command: str)                # total=True
```

### 6.2a Docker base-image sharing across worktrees

Teatree builds each `BaseImageConfig` exactly once on the main repo and
reuses it across every worktree that needs it. Worktrees get code-level
isolation via a `.:/app:rw` volume mount; the image itself is shared.

- Image tag is `{image_name}:deps-{sha256(lockfile)[:12]}` — stable while the
  lockfile is unchanged, new tag when dependencies change.
- `teatree.docker.build.ensure_base_image(cfg)` probes via `docker image
  inspect` and skips the build when the tag already exists locally.
- `image_tag_for_lockfile(cfg)` is pure (just reads and hashes the lockfile)
  and safe to call from env-cache rendering and drift detection.
- `worktree provision` calls `ensure_base_image` once per `(image_name,
  build_context)` pair across the ticket's worktrees, then writes each
  `env_var={tag}` into the per-worktree env cache so compose files can
  reference `image: ${MYAPP_BASE_IMAGE}` without knowing the tag in advance.
- `get_docker_services(worktree)` returns a `set[str]` of service names
  (keys of `get_services_config`) that MUST run in Docker; `worktree provision`
  fails fast if any listed service is not also declared in
  `get_services_config`.
- Both hooks default to empty — existing overlays keep working. Core
  enforcement only activates for overlays that opt in.

### 6.3 Scaffold (`t3 startoverlay`)

`t3 startoverlay <name> <dest>` generates a lightweight overlay package. Default overlay app name is `t3_overlay` (the `t3_` prefix is a convention). The skill directory is derived: `t3_overlay` → skill `overlay` (strip `t3_` prefix and `_overlay` suffix, then `t3:` prefix).

Generated structure:

```
<name>/
  src/t3_overlay/__init__.py, overlay.py, apps.py
  skills/overlay/SKILL.md
  pyproject.toml, .editorconfig, .pre-commit-config.yaml, ...
```

No manage.py, settings.py, urls.py, or wsgi/asgi — teatree is the Django project.

### 6.4 Discovery & Loading

**User-level discovery** (`config.py`):

1. `~/.teatree.toml` `[overlays.<name>]` sections (reads `path`, extracts `DJANGO_SETTINGS_MODULE` from `manage.py`)
2. `teatree.overlays` entry-point group from installed packages
3. Toml wins on name conflicts

**Active overlay selection** (`discover_active_overlay()`):

1. Priority 1: `manage.py` in cwd ancestors (developer working inside project)
2. Priority 2: Single installed overlay (exactly one exists)
3. Returns None if ambiguous

**Django-level loading** (`overlay_loader.py`):

- Discovers overlays via `importlib.metadata.entry_points(group="teatree.overlays")`
- Each entry point name is the overlay name; the value is an overlay class path (e.g., `"myapp.overlay:MyOverlay"`)
- Validates each class is a subclass of `OverlayBase`, then instantiates it
- Supports multiple overlays: `get_overlay(name)` returns one by name (or the sole overlay if only one exists), `get_all_overlays()` returns all as a `dict[str, OverlayBase]`
- Cached via `lru_cache(maxsize=1)` on `_discover_overlays()`, resettable via `reset_overlay_cache()`
- No Django settings involved — no `TEATREE_OVERLAY_CLASS`, no `import_string()`
