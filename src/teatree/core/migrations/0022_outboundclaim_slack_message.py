from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0021_head_carry_over")]

    operations = [
        migrations.AlterField(
            model_name="outboundclaim",
            name="kind",
            field=models.CharField(
                choices=[
                    ("slack_dm", "Slack DM"),
                    ("slack_reaction", "Slack reaction"),
                    ("slack_message", "Slack message"),
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
