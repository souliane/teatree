"""Move stored rows from the retired ``ac-reviewing-codebase`` skill name to ``architectural-review``.

The seed never overwrites an existing row, so without this a box keeps dispatching the
old name. Only the verbatim seeded prompt body is rewritten; an edited one is left alone.
"""

from django.db import migrations

OLD_SKILL = "ac-reviewing-codebase"
NEW_SKILL = "architectural-review"

OLD_PROMPT_BODY = (
    "Run an architectural review of the codebase using the ac-reviewing-codebase skill. "
    "Dispatch a sub-agent that loads /ac-reviewing-codebase and performs a holistic, "
    "codebase-wide architectural review, surfacing findings as the skill prescribes."
)
NEW_PROMPT_BODY = (
    "Run an architectural review of the codebase using the architectural-review skill. "
    "Dispatch a sub-agent that loads /t3:architectural-review and performs a holistic, "
    "codebase-wide architectural review, surfacing findings as the skill prescribes."
)

ARCH_REVIEW = "arch_review"
SKILL_SETTING_KEYS = ("architectural_review_skill", "review_skill")


def _rename(apps, schema_editor, *, skills: tuple[str, str], bodies: tuple[str, str]) -> None:
    old_skill, new_skill = skills
    old_body, new_body = bodies
    db = schema_editor.connection.alias
    prompt_model = apps.get_model("core", "Prompt")
    loop_model = apps.get_model("core", "Loop")
    config_setting = apps.get_model("core", "ConfigSetting")

    old_description, new_description = f"the {old_skill} skill", f"the {new_skill} skill"
    for prompt in prompt_model.objects.using(db).filter(name=ARCH_REVIEW):
        if prompt.body == old_body:
            latest = prompt.versions.using(db).order_by("-version").values_list("version", flat=True).first() or 0
            prompt.versions.db_manager(db).create(
                version=latest + 1, body=prompt.body, params=list(prompt.params or [])
            )
            prompt.body = new_body
        prompt.description = prompt.description.replace(old_description, new_description)
        prompt.save(using=db, update_fields=["body", "description", "updated_at"])
    for loop in loop_model.objects.using(db).filter(name=ARCH_REVIEW):
        loop.description = loop.description.replace(old_description, new_description)
        loop.save(using=db, update_fields=["description"])

    renamed = {old_skill: new_skill, f"t3:{old_skill}": f"t3:{new_skill}"}
    for row in config_setting.objects.using(db).filter(key__in=SKILL_SETTING_KEYS):
        if not isinstance(row.value, str) or row.value not in renamed:
            continue
        row.value = renamed[row.value]
        row.seed_value = renamed.get(row.seed_value, row.seed_value)
        row.save(using=db, update_fields=["value", "seed_value", "updated_at"])


def forward(apps, schema_editor) -> None:
    _rename(apps, schema_editor, skills=(OLD_SKILL, NEW_SKILL), bodies=(OLD_PROMPT_BODY, NEW_PROMPT_BODY))


def backward(apps, schema_editor) -> None:
    _rename(apps, schema_editor, skills=(NEW_SKILL, OLD_SKILL), bodies=(NEW_PROMPT_BODY, OLD_PROMPT_BODY))


class Migration(migrations.Migration):
    dependencies = [("core", "0118_loop_consecutive_deadline_kills")]

    operations = [migrations.RunPython(forward, backward)]
