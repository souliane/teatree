"""Report stored rows left unresolved by migrations 0124 and 0125."""

from itertools import starmap

import typer
from django.apps import apps


def _invalid_private_entry(entry: object) -> bool:
    if not isinstance(entry, str):
        return True
    canonical = entry.rstrip("/").lower()
    canonical = canonical.removesuffix(".git") if "/" in canonical else canonical
    parts = canonical.split("/")
    return (
        "/" not in canonical
        or "." not in parts[0]
        or any(not part or part in {".", ".."} for part in parts)
        or any(char.isspace() or char in "*?[]{}\\:@#%" for char in canonical)
    )


def _session_phase_unresolved(visited: object, visits: object) -> bool:
    visited = visited or []
    visits = visits or {}
    return (
        not isinstance(visited, list)
        or any(not isinstance(phase, str) for phase in visited)
        or not isinstance(visits, dict)
        or any(not isinstance(phase, str) for phase in visits)
    )


def _pr_branch_unresolved(extra: object) -> bool:
    if not isinstance(extra, dict):
        return bool(extra)
    urls = extra.get("pr_urls") or []
    if not isinstance(urls, list):
        return True
    if not urls:
        return False
    index = extra.get("pr_url_by_branch") or {}
    return (
        not isinstance(index, dict)
        or any(not isinstance(url, str) for url in urls)
        or any(url not in index.values() for url in urls)
    )


def _emit(count: int, condition: str, remedy: str) -> bool:
    if count:
        typer.echo(f"FAIL  {count} {condition}. {remedy}")
    return count == 0


def check_blank_work_overlays() -> bool:
    """Recompute every unresolved condition retained by the two migrations."""
    config = apps.get_model("core", "ConfigSetting").objects
    tickets = apps.get_model("core", "Ticket").objects
    sessions = apps.get_model("core", "Session").objects
    worktrees = apps.get_model("core", "Worktree").objects
    results = []
    for name, rows in (("Ticket", tickets), ("Session", sessions), ("Worktree", worktrees)):
        results.append(
            _emit(
                rows.filter(overlay="").count(),
                f"blank-overlay {name} row(s)",
                "Identify the owning overlay from the ticket and repositories; set the overlay on all affected rows.",
            )
        )
    results.extend(
        (
            _emit(
                sum(
                    starmap(
                        _session_phase_unresolved, sessions.values_list("visited_phases", "phase_visits").iterator()
                    )
                ),
                "unresolved session phase row(s)",
                "Replace malformed visited_phases with a list of names and phase_visits with a name-to-record object.",
            ),
            _emit(
                sum(_pr_branch_unresolved(extra) for extra in tickets.values_list("extra", flat=True).iterator()),
                "unresolved PR branch row(s)",
                "Repair pr_urls and pr_url_by_branch so every PR URL has a source branch.",
            ),
            _emit(
                sum(
                    bool(extra and not isinstance(extra, dict))
                    or bool(isinstance(extra, dict) and extra.get("dream_gap_key"))
                    for extra in tickets.values_list("extra", flat=True).iterator()
                ),
                "unresolved dream ticket row(s)",
                "Repair the legacy gap key, owner, and batch lists; convert it into dream_gap_batch "
                "and remove legacy keys.",
            ),
        )
    )
    scope_collisions = sum(
        config.filter(scope="t3-teatree", key=key).exists()
        for key in config.filter(scope="teatree").values_list("key", flat=True).iterator()
    )
    results.append(
        _emit(
            scope_collisions,
            "unresolved setting scope collision row(s)",
            "Merge each short-scope value into its t3-teatree target, then delete the short-scope row.",
        )
    )
    private = config.filter(scope="", key="private_repos").values_list("value", flat=True).first()
    blocked = private is not None and not isinstance(private, list)
    results.append(
        _emit(
            int(blocked),
            "non-list global private_repos row(s)",
            "Replace the global private_repos value with a list of host-qualified repositories.",
        )
    )
    invalid = 0
    uncarried = 0
    for value in config.filter(key="internal_publish_namespaces").values_list("value", flat=True).iterator():
        uncarried += 1
        invalid += sum(_invalid_private_entry(entry) for entry in value) if isinstance(value, list) else 1
    results.extend(
        (
            _emit(
                invalid,
                "invalid legacy private namespace entry/row(s)",
                "Correct or remove invalid internal_publish_namespaces entries, then carry valid entries "
                "into global private_repos.",
            ),
            _emit(
                uncarried,
                "legacy private namespace row(s) left uncarried",
                "Repair global private_repos if it is not a list, carry every retained namespace entry into it, "
                "then delete the legacy rows.",
            ),
        )
    )
    return all(results)
