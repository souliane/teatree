# BLUEPRINT Appendix — Dependencies & Key Conventions

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §15–§16.

## 15. Dependencies

```toml
django>=5.2,<6.1
django-tasks-db>=0.12
django-fsm-2>=4
django-rich>=2.2
django-tasks>=0.9
django-typer>=3.3
httpx>=0.27
```

Dev dependencies: ruff, pytest, pytest-cov, pytest-django, ty, import-linter, prek, safety, typer, django-types.

---

## 16. Key Conventions

- Python 3.13+. Use `X | Y` union syntax, never `Optional`.
- `from __future__ import annotations` is banned.
- No docstrings on classes/methods by policy. Self-documenting code.
- Management commands use `django-typer`, not `BaseCommand`.
- Package is `teatree` (double-e), repo/CLI is `teatree`/`t3`.
- `DJANGO_SETTINGS_MODULE` is stripped from env when running `_managepy()` so the overlay's own settings win.
- **Port allocation is ephemeral (Non-Negotiable).** Host ports are **auto-mapped by Docker** at `worktree start` — the compose override declares container ports with no host binding (`ports: ["<container_port>"]`). After compose up, `WorktreeStartRunner` queries the running project via `docker compose port` and stores the result on `Worktree.extra["ports"]`. Ports are **never** written to `.t3-cache/.t3-env.cache`, the database, or any other persistent store. Docker services are discoverable via `docker compose port` (single source of truth). Inter-service traffic uses compose service DNS — no host port involved.
- Coverage omits only migrations. Everything else must be covered.
- `claude -p` is headless (exits immediately). The user's interactive session running `/loop` is the only persistent Claude Code session.
- Statusline state is rendered to a file (`${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt`, override via `TEATREE_STATUSLINE_FILE`) by the loop and `cat`-ed by the hook — the hook itself does no DB or network I/O. The file lives under XDG data, not `~/.teatree` (which is the user's shell config file, not a directory).
- Overlay-specific names (customer, tenant, product) **must not appear** in `src/teatree/` or `docs/`. The CI grep gate (`scripts/hooks/check_no_overlay_leak.py`) enforces this — forbidden terms are loaded at runtime from `$TEATREE_OVERLAY_LEAK_TERMS` or `~/.teatree.toml` `[overlay_leak].terms` so the public repo never holds tenant names.
- E2E tests (when overlays declare them) use file-based SQLite (not `:memory:`) because Playwright spawns a separate server process.
