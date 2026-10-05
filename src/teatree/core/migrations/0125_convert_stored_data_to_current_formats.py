"""Convert formats; reverses lose data; only rollback restores pre-roll SQLite .backup with previous image."""

import hashlib
import logging
from datetime import time
from urllib.parse import urlparse

from django.db import migrations

logger = logging.getLogger(__name__)

_VALUE_RENAMES = {"low": "slow", "normal": "medium", "high": "full"}
# Frozen at migration creation: the core's own overlay entry point.
_OVERLAY_NAMES = {"teatree": "t3-teatree"}
# Frozen copy of the shipped loop seed; this upgrade refreshes rows from older installs.
_SEEDED_LOOP_DESCRIPTIONS = {
    "inbox": (
        "Drains inbound Slack mentions, DMs, review-intent and RED-CARD reactions (plus the Notion view) into the DB "
        "every 1m and routes them."
    ),
    "idle_stack_reaper": "Stops local dev stacks left idle past their threshold to free a concurrency slot; checks every 1m.",
    "local_stack_queue": (
        "Drains the local-stack acquisition queue, starting the next queued worktree stack whose backoff retry is "
        "due; checks every 1m."
    ),
    "resource_pressure": (
        "Auto-frees host disk and RAM when they cross the pressure threshold; checks every 1m on its own ~5m "
        "internal cadence."
    ),
    "snapshot_warmer": (
        "Refreshes each overlay-declared reference DB's DSLR snapshot out-of-band once a day so a "
        "ticket-critical-path provision never pays the slow restore+migrate path."
    ),
    "dispatch": (
        "Runs the always-on global scanners every 5m: dispatches pending headless Tasks to phase sub-agents, ingests "
        "incoming events, redelivers undelivered notifies, and posts deferred questions."
    ),
    "tickets": (
        "Scans the local Ticket DB and each code host every 5m — surfacing active and stale tickets, dispositioning "
        "issues, and marking completed ones."
    ),
    "review": (
        "Reviews open PRs every 5m and posts inline findings via t3:reviewer — your OWN PRs always (per-SHA "
        "deduped), plus colleague-authored PRs when admit_colleague_prs_to_board is on. The away-gate never skips it "
        "(colleague_facing = false), so self-review keeps going while the owner is unreachable."
    ),
    "ship": (
        "Sweeps your own-authored + same-repo open PRs every 5m: folds in approvals/CI, arms the cold review, and "
        "executes the keystone merge (consumes the orchestrator's MergeClear). Runs under autonomous_away so the "
        "merge path never starves."
    ),
    "issue_disposition": (
        "Auto-closes high-confidence DEAD backlog issues (already-shipped / duplicate / obsolete) every 5m, only for "
        "t3-teatree owned repos; bounded per tick."
    ),
    "audit": "Verifies and posts per-overlay failed-E2E results to Slack (driven by overlay watchers) every 30m.",
    "followup": "Intakes newly-assigned issues (auto-starting ready ones) and fires the review-request nag every 30m.",
    "issue_implementer": (
        "Discovers and claims admitted backlog issues to auto-implement, kicking off the maker pipeline; every 30m. "
        "The active preset decides whether it runs."
    ),
    "triage_assessor": (
        "Assesses OPEN needs-triage issues daily and queues keep/close/needs-info recommendations behind an "
        "ask-gate; this row IS the cadence, the active preset decides whether it runs, and it never acts without "
        "per-item approval."
    ),
    "dm_sweep": (
        "Sweeps the owner's DM threads hourly and resolves the ones that no longer need them (owner already replied, "
        "subject merged/closed, duplicate of an open thread); leaves anything older than a day for the resurfacing "
        "side, and says nothing when it resolved nothing."
    ),
    "housekeeping": (
        "Fast-forwards the editable teatree and overlay installs (self-update), pulls each overlay's main clone, and "
        "reconciles the ticket board against forge truth, hourly."
    ),
    "arch_review": (
        "Dispatches a sub-agent at 04:00 to run a holistic, codebase-wide architectural review via the "
        "architectural-review skill; the scanner enforces architectural_review_cadence_hours (168) and the "
        "merge-count backstop, so this row is only how often that gate is CHECKED — and, because a failed review "
        "leaves that clock untouched, how soon a failed one retries."
    ),
    "dogfood": "Runs the overlay provisioning smoke test once a day to catch broken worktree setup.",
    "eval_local": "Runs the local behavioral eval suite weekly; this row IS the cadence.",
    "db_backup": (
        "Backs up teatree's own control DB at 02:00 and prunes past the keep-last-N-days retention; this row IS the "
        "cadence."
    ),
    "backlog_sweep": (
        "Groups the backlog daily — bundles related issues into an existing host and closes nothing for real; this "
        "row plus the active preset are the switch, gated by ask_before_backlog_sweep_closes."
    ),
    "news": "Fires the daily news-scan task at 08:00 to surface relevant external releases and improvement ideas.",
    "dream": (
        "Runs the nightly memory-consolidation pass at 03:00 — cross-link, merge, reindex MEMORY.md, decay — off the "
        "live tick."
    ),
    "outer_loop": (
        "Advances at most one T4 autoresearch experiment one step per day (propose, ratify, implement, measure, "
        "keep-only-if-better), off the live tick; requires trustworthy "
        "score signals."
    ),
    "directive_loop": (
        "Hourly, off the live tick: interprets captured owner directives up to directive_intake_per_tick per pass "
        "and stops at the human ratify gate, then advances one ratified directive one step (implement, configure, "
        "verify, keep-only-if-verified, else human-asked revert); the execution arc additionally needs trusted score "
        "signals."
    ),
    "ci_eval_heal": (
        "Advances operator-opened CI-eval heal sessions every 5m: dispatch the behavioral eval in CI, poll, and fix "
        "confirmed reds within the session budget and anti-cheat gate. The loop row controls the cadence."
    ),
    "ratchet_repair": (
        "Reads the teatree core clone every 30m and reports reference-ratchet pins the tree no longer resolves, "
        "naming the one-command repair. Observe-only: it writes nothing and opens nothing."
    ),
    "memory_skim": (
        "Skims the Claude memories weekly and raises ONE promote-or-drop question naming every memory that reads as "
        "factory behaviour; the scanner dedupes on the ISO week."
    ),
}
_CURRENT_SHIPPED_CADENCES = {
    "inbox": (60, None),
    "idle_stack_reaper": (60, None),
    "local_stack_queue": (60, None),
    "resource_pressure": (60, None),
    "snapshot_warmer": (86400, None),
    "dispatch": (300, None),
    "tickets": (300, None),
    "review": (300, None),
    "ship": (300, None),
    "issue_disposition": (300, None),
    "audit": (1800, None),
    "followup": (1800, None),
    "issue_implementer": (1800, None),
    "triage_assessor": (86400, None),
    "dm_sweep": (3600, None),
    "housekeeping": (3600, None),
    "arch_review": (86400, time(4, 0)),
    "dogfood": (86400, None),
    "eval_local": (604800, None),
    "db_backup": (86400, time(2, 0)),
    "backlog_sweep": (86400, None),
    "news": (86400, time(8, 0)),
    "dream": (86400, time(3, 0)),
    "outer_loop": (86400, None),
    "directive_loop": (3600, None),
    "ci_eval_heal": (300, None),
    "ratchet_repair": (1800, None),
    "memory_skim": (604800, None),
}
# All other names shipped only the cadence in _CURRENT_SHIPPED_CADENCES.
_OLDER_SHIPPED_CADENCES = {
    "triage_assessor": {(3600, None)},
    "arch_review": {(10800, None)},
    "eval_local": {(86400, None)},
    "db_backup": {(86400, None)},
}
_PHASE_RENAMES = {
    "plan": "planning",
    "scope": "scoping",
    "code": "coding",
    "test": "testing",
    "review": "reviewing",
    "ship": "shipping",
    "retrospect": "retro",
    "retrospecting": "retro",
    "request_review": "requesting_review",
    "request-review": "requesting_review",
    "e2e-review": "e2e_reviewing",
    "e2e_review": "e2e_reviewing",
}


def canonicalize_wip(apps, schema_editor):
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    for row in rows.filter(key__in=("speed", "wip")):
        value = row.value
        canonical_value = _VALUE_RENAMES.get(value.strip().lower(), value) if isinstance(value, str) else value
        seed_value = row.seed_value
        canonical_seed = (
            _VALUE_RENAMES.get(seed_value.strip().lower(), seed_value) if isinstance(seed_value, str) else seed_value
        )
        if row.key == "speed":
            if not rows.filter(scope=row.scope, key="wip").exists():
                rows.create(
                    scope=row.scope,
                    key="wip",
                    value=canonical_value,
                    seeded_by=row.seeded_by,
                    seed_value=canonical_seed,
                    written_by=row.written_by,
                )
            rows.filter(pk=row.pk).delete()
        elif canonical_value != value or canonical_seed != seed_value:
            rows.filter(pk=row.pk).update(value=canonical_value, seed_value=canonical_seed)


def canonicalize_session_phases(apps, schema_editor):
    """Convert phase JSON written before the canonical write boundary existed."""
    sessions = apps.get_model("core", "Session").objects.using(schema_editor.connection.alias)
    unresolved = 0
    for session in sessions.iterator():
        visited = session.visited_phases or []
        visits = session.phase_visits or {}
        if (
            not isinstance(visited, list)
            or any(not isinstance(phase, str) for phase in visited)
            or not isinstance(visits, dict)
            or any(not isinstance(phase, str) for phase in visits)
        ):
            unresolved += 1
            continue
        canonical_visited = list(dict.fromkeys(_PHASE_RENAMES.get(phase, phase) for phase in visited))
        canonical_visits = {}
        for phase, record in visits.items():
            canonical = _PHASE_RENAMES.get(phase, phase)
            if canonical not in canonical_visits or phase == canonical:
                canonical_visits[canonical] = record
        if canonical_visited != visited or canonical_visits != visits:
            sessions.filter(pk=session.pk).update(visited_phases=canonical_visited, phase_visits=canonical_visits)
    _report_unresolved("session phase", unresolved)


def _canonical_overlay(name: str) -> str:
    return _OVERLAY_NAMES.get(name, name)


def _report_unresolved(kind: str, count: int) -> None:
    logger.info("0125: %s unresolved %s row(s) left untouched", count, kind)


def convert_overlay_names(apps, schema_editor):
    """Canonicalize registry keys, DB scopes, and attributed overlay columns."""
    alias = schema_editor.connection.alias
    settings = apps.get_model("core", "ConfigSetting").objects.using(alias)
    _convert_setting_scopes(settings)
    _convert_overlay_registry(settings)
    _convert_overlay_columns(apps, alias)


def _convert_setting_scopes(settings) -> None:
    unresolved = 0
    for row in settings.exclude(scope="").iterator():
        canonical = _canonical_overlay(row.scope)
        if canonical == row.scope:
            continue
        target = settings.filter(scope=canonical, key=row.key).first()
        if target is not None:
            if isinstance(row.value, list) and isinstance(target.value, list):
                merged = list(target.value)
                for entry in row.value:
                    if entry not in merged:
                        merged.append(entry)
                settings.filter(pk=target.pk).update(value=merged)
                settings.filter(pk=row.pk).delete()
            else:
                unresolved += 1
        else:
            settings.filter(pk=row.pk).update(scope=canonical)
    _report_unresolved("setting scope collision", unresolved)


def _convert_overlay_registry(settings) -> None:
    for row in settings.filter(key="overlays").iterator():
        if not isinstance(row.value, dict):
            continue
        converted = {}
        for name, fields in row.value.items():
            canonical = _canonical_overlay(name)
            if canonical == name:
                converted[canonical] = fields
        for name, fields in row.value.items():
            canonical = _canonical_overlay(name)
            if canonical not in converted:
                converted[canonical] = fields
            elif canonical != name and isinstance(fields, dict) and isinstance(converted[canonical], dict):
                converted[canonical] = fields | converted[canonical]
        if converted != row.value:
            settings.filter(pk=row.pk).update(value=converted)


def _convert_overlay_columns(apps, alias: str) -> None:
    for model in apps.get_app_config("core").get_models():
        if not any(field.name == "overlay" for field in vars(model)["_meta"].fields):
            continue
        rows = model.objects.using(alias)
        for pk, overlay in rows.exclude(overlay="").values_list("pk", "overlay").iterator():
            canonical = _canonical_overlay(overlay)
            if canonical != overlay:
                rows.filter(pk=pk).update(overlay=canonical)


def convert_agent_skill_models(apps, schema_editor):
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    for row in rows.filter(key="agent_skill_models").iterator():
        if not isinstance(row.value, dict):
            continue
        converted = {
            skill: value
            if isinstance(value, list)
            else ([] if str(value).strip().lower() in {"", "default", "inherit"} else [{"floor": str(value).strip()}])
            for skill, value in row.value.items()
        }
        if converted != row.value:
            rows.filter(pk=row.pk).update(value=converted)


def _repo_for_pr_url(url: str) -> str:
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    for marker in ("/-/merge_requests/", "/pull/"):
        if marker in path:
            return path.split(marker, maxsplit=1)[0].rsplit("/", maxsplit=1)[-1]
    return ""


def _branch_for_pr_url(ticket, url: str, urls: list[str], extra: dict, worktrees) -> str:
    payload = (extra.get("prs") or {}).get(url) if isinstance(extra.get("prs"), dict) else None
    if isinstance(payload, dict):
        branch = payload.get("source_branch") or payload.get("head_ref")
        if isinstance(branch, str) and branch:
            return branch
    repo = _repo_for_pr_url(url)
    candidates = (
        set(worktrees.filter(ticket_id=ticket.pk, repo_path__endswith=repo).values_list("branch", flat=True))
        if repo
        else set()
    )
    if len(candidates) == 1:
        return candidates.pop()
    if len(urls) == 1:
        fallback = extra.get("ship_invoking_branch") or extra.get("branch")
        return fallback if isinstance(fallback, str) else ""
    return ""


def _index_missing_pr_urls(ticket, urls: list[str], extra: dict, worktrees, existing: dict) -> dict | None:
    by_branch = dict(existing)
    for url in (url for url in urls if url not in by_branch.values()):
        try:
            urlparse(url)
        except ValueError:
            return None
        branch = _branch_for_pr_url(ticket, url, urls, extra, worktrees)
        if not branch or (branch in by_branch and by_branch[branch] != url):
            return None
        by_branch[branch] = url
    return by_branch


def convert_branch_urls(apps, schema_editor):
    """Associate each recorded PR URL with its source branch."""
    alias = schema_editor.connection.alias
    tickets = apps.get_model("core", "Ticket").objects.using(alias)
    worktrees = apps.get_model("core", "Worktree").objects.using(alias)
    unresolved = 0
    for ticket in tickets.iterator():
        if ticket.extra and not isinstance(ticket.extra, dict):
            unresolved += 1
            continue
        extra = dict(ticket.extra or {})
        urls = extra.get("pr_urls") or []
        if not isinstance(urls, list):
            unresolved += 1
            continue
        if not urls:
            continue
        existing = extra.get("pr_url_by_branch") or {}
        if not isinstance(existing, dict) or any(not isinstance(url, str) for url in urls):
            unresolved += 1
            continue
        by_branch = _index_missing_pr_urls(ticket, urls, extra, worktrees, existing)
        if by_branch is None:
            unresolved += 1
            continue
        if by_branch != extra.get("pr_url_by_branch"):
            extra["pr_url_by_branch"] = by_branch
            tickets.filter(pk=ticket.pk).update(extra=extra)
    _report_unresolved("PR branch", unresolved)


def refresh_seeded_loop_descriptions(apps, schema_editor):
    loops = apps.get_model("core", "Loop").objects.using(schema_editor.connection.alias)
    for name, description in _SEEDED_LOOP_DESCRIPTIONS.items():
        current = _CURRENT_SHIPPED_CADENCES[name]
        for loop in loops.filter(name=name):
            cadence = (loop.delay_seconds, loop.daily_at)
            if cadence == current:
                if loop.description != description:
                    loops.filter(pk=loop.pk).update(description=description)
            elif cadence in _OLDER_SHIPPED_CADENCES.get(name, ()):
                loops.filter(pk=loop.pk).update(delay_seconds=current[0], daily_at=current[1], description=description)


def _one_overlay(candidates: set[str]) -> str:
    candidates.discard("")
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _issue_repo_slugs(url: str) -> set[str]:
    try:
        path = urlparse(url).path.strip("/")
    except ValueError:
        return set()
    for marker in ("/-/issues/", "/issues/"):
        if marker in f"/{path}":
            full = path.split(marker.lstrip("/"), maxsplit=1)[0].rstrip("/")
            return {full, full.rsplit("/", maxsplit=1)[-1]}
    return set()


def convert_blank_work_overlays(apps, schema_editor):
    """Stamp work rows previously admitted through the shared blank-overlay arm."""
    alias = schema_editor.connection.alias
    tickets = apps.get_model("core", "Ticket").objects.using(alias)
    sessions = apps.get_model("core", "Session").objects.using(alias)
    worktrees = apps.get_model("core", "Worktree").objects.using(alias)
    settings = apps.get_model("core", "ConfigSetting").objects.using(alias)
    registry = settings.filter(scope="", key="overlays").values_list("value", flat=True).first() or {}
    configured = set(registry) if isinstance(registry, dict) else set()
    default = configured
    unresolved = 0
    for ticket in tickets.filter(overlay="").iterator():
        candidates = set(worktrees.filter(ticket_id=ticket.pk).exclude(overlay="").values_list("overlay", flat=True))
        candidates.update(sessions.filter(ticket_id=ticket.pk).exclude(overlay="").values_list("overlay", flat=True))
        if not candidates:
            repos = _issue_repo_slugs(ticket.issue_url or "")
            for name, fields in registry.items() if isinstance(registry, dict) else ():
                workspace_repos = fields.get("workspace_repos") if isinstance(fields, dict) else None
                if isinstance(workspace_repos, list) and repos.intersection(
                    repo for repo in workspace_repos if isinstance(repo, str)
                ):
                    candidates.add(name)
        if not candidates:
            candidates = set(default)
        target = _one_overlay(candidates)
        if target:
            tickets.filter(pk=ticket.pk).update(overlay=target)
        else:
            unresolved += 1
    for rows in (sessions, worktrees):
        for row in rows.filter(overlay="").iterator():
            ticket_overlay = (
                tickets.filter(pk=row.ticket_id).values_list("overlay", flat=True).first() if row.ticket_id else ""
            )
            target = _one_overlay({ticket_overlay} if ticket_overlay else set(default))
            if target:
                rows.filter(pk=row.pk).update(overlay=target)
            else:
                unresolved += 1
    _report_unresolved("work overlay", unresolved)


def convert_identity_groups(apps, schema_editor):
    """Copy the owner's flat aliases into the grouped overlay policy once."""
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    registry_row = rows.filter(scope="", key="overlays").first()
    if registry_row is None or not isinstance(registry_row.value, dict):
        return
    global_aliases = rows.filter(scope="", key="user_identity_aliases").values_list("value", flat=True).first() or []
    registry = dict(registry_row.value)
    changed = False
    for name, fields in registry.items():
        if not isinstance(fields, dict):
            continue
        current = fields.get("identity_aliases")
        if isinstance(current, list) and current and all(isinstance(item, str) for item in current):
            registry[name] = fields | {"identity_aliases": [current]}
            changed = True
            continue
        if current:
            continue
        scoped = rows.filter(scope=name, key="user_identity_aliases").values_list("value", flat=True).first()
        aliases = scoped if scoped is not None else global_aliases
        if isinstance(aliases, list) and aliases:
            registry[name] = fields | {"identity_aliases": [aliases]}
            changed = True
    if changed:
        rows.filter(pk=registry_row.pk).update(value=registry)


def convert_dream_tickets(apps, schema_editor):
    """Fold old single-gap tickets into the batch ledger of their owning ticket."""
    alias = schema_editor.connection.alias
    tickets = apps.get_model("core", "Ticket").objects.using(alias)
    memories = apps.get_model("core", "ConsolidatedMemory").objects.using(alias)
    unresolved = 0
    for ticket in tickets.iterator():
        if ticket.extra and not isinstance(ticket.extra, dict):
            unresolved += 1
            continue
        if (ticket.extra or {}).get("dream_gap_key") and not _convert_one_dream_ticket(tickets, memories, ticket):
            unresolved += 1
    _convert_dream_anchors(apps, alias)
    _report_unresolved("dream ticket", unresolved)


def _dream_target(tickets, ticket):
    extra = ticket.extra or {}
    gap_key = extra["dream_gap_key"]
    cluster_key = extra.get("dream_memory_cluster_key") or gap_key
    owner_pk = extra.get("dream_gap_folded_into") or ticket.pk
    if not isinstance(gap_key, str) or not isinstance(cluster_key, str) or not isinstance(owner_pk, int):
        return None
    owner = tickets.filter(pk=owner_pk).first()
    if owner is None or (owner.extra and not isinstance(owner.extra, dict)):
        return None
    owner_extra = dict(owner.extra or {})
    if not isinstance(owner_extra.get("dream_gap_batch") or [], list) or not isinstance(
        owner_extra.get("dream_gap_claimed_delivered") or [], list
    ):
        return None
    return owner, owner_extra, gap_key, cluster_key


def _convert_one_dream_ticket(tickets, memories, ticket) -> bool:
    target = _dream_target(tickets, ticket)
    if target is None:
        return False
    owner, owner_extra, gap_key, cluster_key = target
    extra = dict(ticket.extra or {})
    batch = list(owner_extra.get("dream_gap_batch") or [])
    if not any(isinstance(entry, dict) and entry.get("gap_key") == gap_key for entry in batch):
        citation = memories.filter(cluster_key=cluster_key).values_list("verified_citation", flat=True).first()
        batch.append(
            {
                "gap_key": gap_key,
                "cluster_key": cluster_key,
                "title": ticket.short_description,
                "citation": "cited" if isinstance(citation, str) and citation.strip() else "uncited",
            }
        )
        owner_extra["dream_gap_batch"] = batch
    if extra.get("dream_umbrella_url") and not owner_extra.get("dream_umbrella_url"):
        owner_extra["dream_umbrella_url"] = extra["dream_umbrella_url"]
    delivered = list(owner_extra.get("dream_gap_claimed_delivered") or [])
    if gap_key not in delivered:
        delivered.append(gap_key)
        owner_extra["dream_gap_claimed_delivered"] = delivered
    for key in ("dream_gap_key", "dream_memory_cluster_key", "dream_gap_folded_into"):
        extra.pop(key, None)
    if owner.pk == ticket.pk:
        for key in ("dream_gap_key", "dream_memory_cluster_key", "dream_gap_folded_into"):
            owner_extra.pop(key, None)
        tickets.filter(pk=ticket.pk).update(extra=owner_extra)
    else:
        tickets.filter(pk=owner.pk).update(extra=owner_extra)
        tickets.filter(pk=ticket.pk).update(extra=extra)
    return True


def _convert_dream_anchors(apps, alias: str) -> None:
    memories = apps.get_model("core", "ConsolidatedMemory").objects.using(alias)
    for row in memories.filter(ticket_url__contains="#dream-gap=").iterator():
        base, _, gap_key = row.ticket_url.partition("#dream-gap=")
        digest = hashlib.sha256(gap_key.encode()).hexdigest()[:16]
        memories.filter(pk=row.pk).update(ticket_url=f"{base}#dream-batch={digest}")


class Migration(migrations.Migration):
    dependencies = [("core", "0124_delete_rows_of_retired_settings")]

    operations = [
        migrations.RunPython(canonicalize_wip, migrations.RunPython.noop),
        migrations.RunPython(canonicalize_session_phases, migrations.RunPython.noop),
        migrations.RunPython(convert_overlay_names, migrations.RunPython.noop),
        migrations.RunPython(convert_agent_skill_models, migrations.RunPython.noop),
        migrations.RunPython(convert_blank_work_overlays, migrations.RunPython.noop),
        migrations.RunPython(convert_identity_groups, migrations.RunPython.noop),
        migrations.RunPython(convert_branch_urls, migrations.RunPython.noop),
        migrations.RunPython(convert_dream_tickets, migrations.RunPython.noop),
        migrations.RunPython(refresh_seeded_loop_descriptions, migrations.RunPython.noop),
    ]
