"""Worktree lifecycle state machine.

States: created → provisioned → services_up → ready
Persists to .state.json in the ticket directory.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from lib import registry
from lib.db import db_exists, worktree_db_name
from lib.env import find_free_ports
from lib.fsm import get_available_transitions, transition

_STATE_FILENAME = ".state.json"


def _write_env_worktree(facts: dict, ticket_dir: str) -> str:
    """Write the ticket-level .env.worktree from lifecycle facts."""
    ports = facts.get("ports", {})
    envfile = str(Path(ticket_dir) / ".env.worktree")
    with Path(envfile).open("w", encoding="utf-8") as f:
        f.write(f"WT_VARIANT={facts.get('variant', '')}\n")
        f.write(f"TICKET_DIR={ticket_dir}\n")
        f.write(f"WT_DB_NAME={facts.get('db_name', '')}\n")
        f.write(f"BACKEND_PORT={ports.get('backend', 0)}\n")
        f.write(f"FRONTEND_PORT={ports.get('frontend', 0)}\n")
        f.write(f"POSTGRES_PORT={ports.get('postgres', 0)}\n")
        f.write(f"REDIS_PORT={ports.get('redis', 0)}\n")
        f.write(f"BACK_END_URL=http://localhost:{ports.get('backend', 0)}\n")
        f.write(f"FRONT_END_URL=http://localhost:{ports.get('frontend', 0)}\n")
        f.write(f"COMPOSE_PROJECT_NAME={facts.get('compose_name', '')}\n")
    return envfile


def _link_repo_env_worktree(wt_dir: str, ticket_dir: str) -> None:
    """Symlink repo-level .env.worktree → ticket-level .env.worktree.

    Docker compose and direnv in the repo directory need .env.worktree
    to be present. The real file lives at the ticket directory level.
    """
    wt_envwt = Path(wt_dir) / ".env.worktree"
    ticket_envwt = Path(ticket_dir) / ".env.worktree"
    if wt_envwt.is_symlink() or wt_envwt.is_file():
        wt_envwt.unlink()
    wt_envwt.symlink_to(ticket_envwt)


def _direnv_load(directory: str) -> None:
    """Use direnv to load the environment for a directory into os.environ.

    Runs ``direnv export json`` from the given directory and merges the
    result into os.environ.  Raises FileNotFoundError if direnv is not
    installed (mandatory dependency).
    """
    result = subprocess.run(
        ["direnv", "export", "json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=directory,
    )
    if result.returncode == 0 and result.stdout.strip():
        env = json.loads(result.stdout)
        os.environ.update(env)


class WorktreeLifecycle:
    """Tracks worktree lifecycle state with guarded transitions."""

    state: str = "created"

    def __init__(self, ticket_dir: str) -> None:
        self.ticket_dir = ticket_dir
        self.facts: dict[str, Any] = {}
        self._load()

    def _state_file(self) -> Path:
        return Path(self.ticket_dir) / _STATE_FILENAME

    def _load(self) -> None:
        sf = self._state_file()
        if sf.is_file():
            data = json.loads(sf.read_text(encoding="utf-8"))
            self.state = data.get("state", "created")
            self.facts = data.get("facts", {})

    def save(self) -> None:
        sf = self._state_file()
        if self.state == "created" and not self.facts:
            # Clean slate — no file needed. Remove if exists.
            if sf.is_file():
                sf.unlink()
            return
        data = {"state": self.state, "facts": self.facts}
        sf.write_text(
            json.dumps(data, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ticket_dir": self.ticket_dir,
            "facts": self.facts,
            "available_transitions": [
                {
                    "method": t["method"],
                    "conditions": [c.__name__ for c in t["conditions"]],
                }
                for t in get_available_transitions(self)
            ],
        }

    @transition(source="created", target="provisioned")
    def provision(self, wt_dir: str, main_repo: str, variant: str) -> None:
        be, fe, pg, rd = find_free_ports(self.ticket_dir)
        self.facts["ports"] = {"backend": be, "frontend": fe, "postgres": pg, "redis": rd}
        self.facts["wt_dir"] = wt_dir
        self.facts["main_repo"] = main_repo
        self.facts["variant"] = variant

        match = re.search(r"\d+", Path(self.ticket_dir).name)
        ticket_number = match.group() if match else "0"
        db_name = worktree_db_name(ticket_number, variant)
        self.facts["db_name"] = db_name

        compose_name = f"oper-product-wt{ticket_number}"
        self.facts["compose_name"] = compose_name

        # Set env vars for subprocess (Docker compose, pg CLI)
        os.environ["COMPOSE_PROJECT_NAME"] = compose_name
        os.environ["POSTGRES_PORT"] = str(pg)
        os.environ.setdefault("POSTGRES_HOST", "localhost")
        os.environ.setdefault("POSTGRES_USER", "local_superuser")
        os.environ.setdefault("POSTGRES_PASSWORD", "local_superpassword")

        # Phase 1: Symlinks
        registry.call("wt_symlinks", wt_dir, main_repo, variant)

        # Phase 2: .env.worktree
        envfile = _write_env_worktree(self.facts, self.ticket_dir)
        registry.call("wt_env_extra", envfile)
        _link_repo_env_worktree(wt_dir, self.ticket_dir)
        subprocess.run(["direnv", "allow", wt_dir], capture_output=True, check=False)
        _direnv_load(wt_dir)

        # Phase 3: Services + DB
        registry.call("wt_services", main_repo, wt_dir)

        if not db_exists(db_name):
            registry.call("wt_db_import", db_name, variant, main_repo)
        registry.call("wt_post_db", wt_dir)

    @transition(source="provisioned", target="services_up")
    def start_services(self) -> None:
        registry.validate_overrides("services")
        wt_dir = self.facts.get("wt_dir", "")
        if wt_dir:
            _direnv_load(wt_dir)
        # Delegate to project overlay's start_session (handles backend fg +
        # frontend bg in parallel).  Fall back to sequential calls.
        try:
            registry.call("wt_start_session")
        except KeyError:
            wt_dir = self.facts.get("wt_dir", "")
            registry.call("wt_run_backend", wt_dir)
            registry.call("wt_run_frontend", wt_dir)

    @transition(source="services_up", target="ready")
    def verify(self) -> None:
        ports = self.facts.get("ports", {})
        self.facts["urls"] = {
            "backend": f"http://localhost:{ports.get('backend', 0)}",
            "frontend": f"http://localhost:{ports.get('frontend', 0)}",
        }

    @transition(source=["provisioned", "services_up", "ready"], target="provisioned")
    def db_refresh(self) -> None:
        db_name = self.facts.get("db_name", "")
        variant = self.facts.get("variant", "")
        main_repo = self.facts.get("main_repo", "")
        wt_dir = self.facts.get("wt_dir", "")
        if db_name and main_repo:
            registry.call("wt_db_import", db_name, variant, main_repo)
            registry.call("wt_post_db", wt_dir)

    @transition(source="*", target="created")
    def teardown(self) -> None:
        self.facts = {}
        # save() is called by the decorator — it detects state=="created" + empty facts
        # and removes the state file instead of writing it.
