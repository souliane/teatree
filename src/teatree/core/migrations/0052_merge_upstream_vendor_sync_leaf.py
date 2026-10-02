# Rejoins the two AST-visible leaves this branch created: the vendor-sync merge
# (0047_merge_upstream_vendor_sync) and the review-request chain's tip. Empty, so it
# only reconciles the graph — the fields themselves stay in the migration that has
# always carried them, which is what keeps already-migrated boxes consistent.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0051_red_mr_fix_attempt_kind"),
        ("core", "0047_merge_upstream_vendor_sync"),
    ]

    operations = []
