# Admin dashboard snapshot

A live "screenshot" of the teatree admin dashboard — the Django admin index listing
every registered `teatree.core` domain model. It is **generated**, not hand-authored:
`scripts/hooks/generate_dashboard_snapshot.py` renders the page through Django's test
client and writes [`admin-index.html`](generated/dashboard/admin-index.html). CI
regenerates it and fails on drift (`git diff --exit-code docs/generated`), so
registering a new admin model updates this snapshot automatically. Edit
`src/teatree/core/admin.py`, not the HTML.

`/admin/` is re-skinned to match `/dash/`: a `templates/admin/base_site.html`
override links the shared design tokens (`dash/css/tokens.css`) and an admin-var
mapping (`dash/css/admin-theme.css`), so the snapshot carries those two
stylesheet links. The generator hook re-fires when either the override or the
theme CSS changes.

<iframe src="../generated/dashboard/admin-index.html" title="teatree admin dashboard snapshot"
        style="width: 100%; height: 640px; border: 1px solid var(--md-default-fg-color--lightest);"></iframe>

[Open the full-page snapshot](generated/dashboard/admin-index.html)
