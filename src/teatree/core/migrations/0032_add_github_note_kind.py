"""Add ``github_note`` to :class:`OutboundClaim.Kind` for #1198."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_ticket_short_description"),
    ]

    operations = [
        migrations.AlterField(
            model_name="outboundclaim",
            name="kind",
            field=models.CharField(
                choices=[
                    ("slack_dm", "Slack DM"),
                    ("slack_reaction", "Slack reaction"),
                    ("gitlab_note", "GitLab note"),
                    ("gitlab_approve", "GitLab approve"),
                    ("github_note", "GitHub note"),
                    ("notion_comment", "Notion comment"),
                    ("notion_edit", "Notion edit"),
                ],
                max_length=32,
            ),
        ),
    ]
