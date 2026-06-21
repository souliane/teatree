"""Repoint each default script loop from the shared ``run.py`` to its OWN module (#2513).

The #2550 cutover pointed every script-backed default :class:`Loop` row at the
SHARED ``src/teatree/loops/run.py``, so the DB ``script`` column was dead — the
live tick drove behaviour off the code registry, not the column. This data
migration makes the column PER-LOOP and load-bearing on an existing DB: each
default script row's ``script`` moves to its OWN module
``src/teatree/loops/<name>/loop.py`` (the file exposing that loop's
``MINI_LOOP``).

Guarded so operator-edited rows are left alone: a row is repointed ONLY when it
still holds the EXACT old shared value AND carries a known default name. A row an
operator has already re-pointed (any other ``script`` value, or a custom name)
is untouched. ``arch_review`` stays prompt-backed; its trivial wrapper prompt
body is replaced with the real ``ac-reviewing-codebase`` instruction (guarded the
same way — only the exact old trivial body is rewritten).

No schema change — ``script`` stays a ``CharField`` and the XOR +
script-requires-delay constraints are unchanged. The cutover stays PAUSED: this
migration never touches ``enabled``. Reversible: the reverse restores the shared
``run.py`` value and the old trivial prompt body from the migrated state.
"""

from django.db import migrations

# Self-contained migration: the per-loop entry point and the new arch_review
# prompt body are inlined here, NOT imported from ``teatree.loops.seed`` — a
# migration is a historical snapshot and must not depend on evolving application
# code (and ``teatree.loops`` is a higher layer than ``teatree.core``). The
# install-time seed keeps the same canonical values; the seed parity test pins
# they agree.
_OLD_SHARED_SCRIPT = "src/teatree/loops/run.py"
_OLD_ARCH_REVIEW_PROMPT_BODY = "Run a sub-agent to run the arch_review loop."
_NEW_ARCH_REVIEW_PROMPT_BODY = (
    "Run an architectural review of the codebase using the ac-reviewing-codebase skill. "
    "Dispatch a sub-agent that loads /ac-reviewing-codebase and performs a holistic, "
    "codebase-wide architectural review, surfacing findings as the skill prescribes."
)


def _own_module(name: str) -> str:
    return f"src/teatree/loops/{name}/loop.py"


# The default script-backed loop names whose rows the #2550 cutover pointed at the
# shared runner. ``arch_review`` (prompt-backed) and ``slack_answer`` (no registry
# MiniLoop, removed by 0091) are intentionally absent.
_DEFAULT_SCRIPT_LOOP_NAMES = frozenset(
    {
        "inbox",
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
        "dispatch",
        "tickets",
        "review",
        "ship",
        "pane_reaper",
        "audit",
        "followup",
        "issue_implementer",
        "housekeeping",
        "dogfood",
        "eval_local",
        "news",
        "dream",
    },
)


def _repoint_to_own_module(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    for name in _DEFAULT_SCRIPT_LOOP_NAMES:
        loop_model.objects.filter(name=name, script=_OLD_SHARED_SCRIPT).update(script=_own_module(name))
    _rewrite_arch_review_prompt(apps, body=_NEW_ARCH_REVIEW_PROMPT_BODY, only_if=_OLD_ARCH_REVIEW_PROMPT_BODY)


def _restore_shared_runner(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    for name in _DEFAULT_SCRIPT_LOOP_NAMES:
        loop_model.objects.filter(name=name, script=_own_module(name)).update(script=_OLD_SHARED_SCRIPT)
    _rewrite_arch_review_prompt(apps, body=_OLD_ARCH_REVIEW_PROMPT_BODY, only_if=_NEW_ARCH_REVIEW_PROMPT_BODY)


def _rewrite_arch_review_prompt(apps, *, body: str, only_if: str) -> None:
    """Set the ``arch_review`` loop's prompt body to *body* iff it still holds *only_if*."""
    loop_model = apps.get_model("core", "Loop")
    prompt_model = apps.get_model("core", "Prompt")
    loop = loop_model.objects.filter(name="arch_review", prompt__isnull=False).select_related("prompt").first()
    if loop is None:
        return
    prompt_model.objects.filter(pk=loop.prompt_id, body=only_if).update(body=body)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0093_collapse_agent_review_request_disabled"),
    ]

    operations = [
        migrations.RunPython(_repoint_to_own_module, _restore_shared_runner),
    ]
