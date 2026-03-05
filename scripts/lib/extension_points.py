"""Default no-op implementations for all extension points.

Registered at the 'default' layer by _init.init().
"""

import os
import sys
from contextlib import suppress
from pathlib import Path


def _backup_envrc(wt_dir: str) -> None:
    envrc = Path(wt_dir) / ".envrc"
    if not envrc.is_file() or envrc.is_symlink():
        return
    envrc.rename(Path(wt_dir) / ".envrc.bak")
    print("  Backed up old generated .envrc to .envrc.bak")


def _replicate_root_symlinks(wt_dir: str, main_repo: str) -> None:
    for entry in os.scandir(main_repo):
        if not entry.is_symlink():
            continue
        link_target = str(Path(entry.path).readlink())
        dest = Path(wt_dir) / entry.name
        if dest.exists() and not dest.is_symlink():
            print(
                f"  Skipping {entry.name} (real file exists, won't replace with symlink)",
                file=sys.stderr,
            )
            continue
        try:
            if dest.is_symlink():
                dest.unlink()
            dest.symlink_to(link_target)
        except OSError:
            pass


def _share_runtime_dirs(wt_dir: str, main_repo: str) -> None:
    shared = os.environ.get("T3_SHARED_DIRS", ".data").split(",")
    for name in (".venv", ".python-version", *shared):
        src = Path(main_repo) / name
        dest = Path(wt_dir) / name
        if src.exists() and not dest.exists():
            with suppress(OSError):
                dest.symlink_to(src)

    nm_src = Path(main_repo) / "node_modules"
    nm_dest = Path(wt_dir) / "node_modules"
    if nm_src.is_dir() and not nm_dest.exists():
        with suppress(OSError):
            nm_dest.symlink_to(nm_src)


def wt_symlinks(wt_dir: str, main_repo: str, _variant: str = "") -> None:
    """Phase 1: Replicate symlinks from main repo + share runtime dirs."""
    _backup_envrc(wt_dir)
    _replicate_root_symlinks(wt_dir, main_repo)
    _share_runtime_dirs(wt_dir, main_repo)
    print("  Symlinks created")


def wt_env_extra(envfile: str) -> None:
    """No-op — project skill appends to envfile."""


def wt_services(main_repo: str, wt_dir: str = "") -> None:
    """Start Docker services from main repo.

    Uses wt_dir as --project-directory so Docker reads the worktree's
    docker-compose.override.yml (with custom port mappings) instead of the
    main repo's.
    """
    project_dir = wt_dir or main_repo
    compose_file = Path(main_repo) / "docker-compose.yml"
    if compose_file.is_file():
        import subprocess

        cmd = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
        ]
        override_file = Path(project_dir) / "docker-compose.override.yml"
        if override_file.is_file():
            cmd.extend(["-f", str(override_file)])
        cmd.extend(
            [
                "--project-directory",
                project_dir,
                "up",
                "-d",
                "--no-build",
            ]
        )
        subprocess.run(
            cmd,
            check=False,
        )


def wt_db_import(_db_name: str, _variant: str, _main_repo: str) -> bool:
    """Return False — no default import. Project skill provides."""
    return False


def wt_post_db(project_dir: str) -> None:
    """No-op — framework or project skill provides."""


def wt_detect_variant(explicit: str = "") -> str:
    """Detect the current variant (tenant/customer/environment)."""
    from lib.env import detect_ticket_dir, read_env_key

    # 1. Explicit argument
    if explicit:
        return explicit
    # 2. Environment variable
    wt_variant = os.environ.get("WT_VARIANT", "")
    if wt_variant:
        return wt_variant
    # 3. .env.worktree in ticket dir
    td = detect_ticket_dir()
    if td:
        envwt = str(Path(td) / ".env.worktree")
        v = read_env_key(envwt, "WT_VARIANT")
        if v:
            return v
    # 4. .env.worktree in current dir
    v = read_env_key(str(Path.cwd() / ".env.worktree"), "WT_VARIANT")
    if v:
        return v
    return ""


def wt_run_backend(*_args: str) -> None:
    print("Define wt_run_backend in your project skill")


def wt_run_frontend(*_args: str) -> None:
    print("Define wt_run_frontend in your project skill")


def wt_build_frontend(*_args: str) -> None:
    print("Define wt_build_frontend in your project skill")


def wt_run_tests(*_args: str) -> None:
    print("Define wt_run_tests in your project skill")


def wt_create_mr(*_args: str) -> None:
    print("Define wt_create_mr in your project skill")


def wt_monitor_pipeline(*_args: str) -> None:
    print("Define wt_monitor_pipeline in your project skill")


def wt_send_review_request(*_args: str) -> None:
    print("Define wt_send_review_request in your project skill")


def wt_fetch_failed_tests(*_args: str) -> None:
    print("Define wt_fetch_failed_tests in your project skill")


def wt_restore_ci_db(*_args: str) -> None:
    """Restore DB from a CI-produced dump. Project skill provides."""
    print("Define wt_restore_ci_db in your project skill")


def wt_reset_passwords(*_args: str) -> None:
    """Reset all user passwords to a known dev value. Project skill provides."""
    print("Define wt_reset_passwords in your project skill")


def wt_trigger_e2e(*_args: str) -> None:
    """Trigger E2E tests on CI. Project skill provides."""
    print("Define wt_trigger_e2e in your project skill")


def wt_quality_check(*_args: str) -> None:
    """Run quality analysis (SonarQube, CodeClimate, etc.). Project skill provides."""
    print("Define wt_quality_check in your project skill")


def wt_fetch_ci_errors(*_args: str) -> None:
    """Fetch error logs from CI (distinct from failed test IDs). Project skill provides."""
    print("Define wt_fetch_ci_errors in your project skill")


def wt_start_session(*_args: str) -> int:
    """Start the full dev session (DB + backend + frontend).

    Project skills should override this with their one-liner entrypoint
    that initializes, self-heals, and runs everything.
    """
    print("Define wt_start_session in your project skill")  # pragma: no cover
    return 1  # pragma: no cover


def ticket_check_deployed(_ticket_iid: str, _mrs: list[dict]) -> bool:
    """Check if merged MRs are deployed to target environment.

    Project skills override with environment-specific checks (CI pipeline,
    GCP, k8s, etc.).
    """
    return False


def ticket_update_external_tracker(
    _ticket_iid: str,
    _new_status: str,
    _project_path: str,
) -> bool:
    """Update ticket status in external tracker (Notion, Jira, etc.).

    Returns True if updated, False if not configured or ticket not found.
    """
    print("  No external tracker configured — skipping")
    return False


def ticket_get_mrs(branch: str, repos: list[str]) -> list[dict]:
    """List MRs for a branch across repos.

    Returns list of dicts with: web_url, state, source_branch, target_branch,
    iid, project_path.
    """
    import json
    import subprocess

    mrs: list[dict] = []
    for repo in repos:
        result = subprocess.run(
            ["glab", "mr", "list", "--source-branch", branch, "-F", "json", "-R", repo],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for mr in json.loads(result.stdout):
                mr["project_path"] = repo
                mrs.append(mr)
    return mrs


def register_defaults() -> None:
    from lib.registry import register

    register("wt_symlinks", wt_symlinks, "default")
    register("wt_env_extra", wt_env_extra, "default")
    register("wt_services", wt_services, "default")
    register("wt_db_import", wt_db_import, "default")
    register("wt_post_db", wt_post_db, "default")
    register("wt_detect_variant", wt_detect_variant, "default")
    register("wt_run_backend", wt_run_backend, "default")
    register("wt_run_frontend", wt_run_frontend, "default")
    register("wt_build_frontend", wt_build_frontend, "default")
    register("wt_run_tests", wt_run_tests, "default")
    register("wt_create_mr", wt_create_mr, "default")
    register("wt_monitor_pipeline", wt_monitor_pipeline, "default")
    register("wt_send_review_request", wt_send_review_request, "default")
    register("wt_fetch_failed_tests", wt_fetch_failed_tests, "default")
    register("wt_restore_ci_db", wt_restore_ci_db, "default")
    register("wt_reset_passwords", wt_reset_passwords, "default")
    register("wt_trigger_e2e", wt_trigger_e2e, "default")
    register("wt_quality_check", wt_quality_check, "default")
    register("wt_fetch_ci_errors", wt_fetch_ci_errors, "default")
    register("wt_start_session", wt_start_session, "default")
    register("ticket_check_deployed", ticket_check_deployed, "default")
    register("ticket_update_external_tracker", ticket_update_external_tracker, "default")
    register("ticket_get_mrs", ticket_get_mrs, "default")
