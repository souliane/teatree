# GitHub App runbook (#4795)

The GitHub App webhook transport, with polling as the always-available fallback.
`Schedule -> active Preset -> enabled GitHub polling loops` (the issue's canonical
control hierarchy) resolves here as: `github_transport_preset` (a DB-home
`ConfigSetting`, per-overlay overridable) gates the `github_polling` scanner; the
signed webhook receiver (`/hooks/github/`) stays reachable under EVERY preset —
preset selection controls polling and downstream processing, never inbound
acceptance.

## App creation

1. Generate the manifest:

   ```bash
   t3 <overlay> github_app manifest \
     --name "teatree-<overlay>" \
     --url "https://<your-box>" \
     --webhook-url "https://<your-box>/hooks/github/" > manifest.json
   ```

   The manifest is deterministic and carries no secrets — review it before
   proceeding. Every permission it requests maps to a named capability
   (`teatree.backends.github.app_manifest.CAPABILITY_FOR_PERMISSION`).

2. Complete App creation in the browser via GitHub's manifest flow: POST the
   manifest JSON to `https://github.com/settings/apps/new?state=<random>` (or an
   org's equivalent `https://github.com/organizations/<org>/settings/apps/new`)
   as a form field named `manifest`, submit, and GitHub redirects to your
   `redirect_url` with a one-time `?code=<code>` query parameter.

3. Exchange the code — this is the ONLY moment GitHub returns the App's secrets,
   and they are written straight to the `pass` store, never echoed:

   ```bash
   t3 <overlay> github_app register <code>
   ```

   Prints only non-secret identity (`app_id`, `slug`, `name`, `html_url`). The
   private key, webhook secret, and client secret land at
   `pass github-app/private-key`, `pass github-app/webhook-secret`,
   `pass github-app/client-secret` respectively.

4. Wire the webhook secret into the deployment's `TEATREE_GITHUB_WEBHOOK_SECRET`
   environment variable (the webhook view resolves it from Django settings, same
   as the pre-#4795 static setup — the App registration step above does not
   change how the webhook view authenticates a delivery, only how the secret was
   obtained):

   ```bash
   pass show github-app/webhook-secret   # copy into your deploy secret manager
   ```

## Installation

Install the App on the desired account from its GitHub App settings page
(`https://github.com/settings/apps/<slug>/installations`), selecting an explicit
repository set (never "All repositories" unless that is genuinely intended).
GitHub fires an `installation` webhook on install; teatree creates a
`GitHubAppInstallation` row and stamps every selected repo into
**`pending_repositories`** — never directly into `active_repositories`.

Confirm which repos the poller and CLI may actually act on:

```bash
t3 <overlay> github_app status                        # see installation_id + pending repos
t3 <overlay> github_app confirm-repos <installation_id> --repo owner/repo   # confirm one
t3 <overlay> github_app confirm-repos <installation_id>                     # confirm ALL pending
```

A later `installation_repositories` event (GitHub reports the account added more
repos) lands the new ones back in `pending_repositories` — access never
broadens silently. A repo GitHub *removes* from the installation is dropped from
both `active_repositories` and `pending_repositories` immediately.

## Key rotation

Generate a new private key from the App's settings page, then re-run the
manifest-flow registration (step 3 above) is NOT how rotation works — GitHub's
key rotation is a separate "Generate a private key" button that downloads a new
PEM without changing `app_id`/`client_id`/`webhook_secret`. Store the new key:

```bash
pass insert --multiline --force github-app/private-key   # paste the new PEM, Ctrl-D
```

The old key stays valid for a short overlap window (per GitHub's own docs) —
verify a poll tick or webhook delivery still authenticates before revoking it
from the App's settings page.

## Delivery testing

With the `webhook` preset NOT yet active (stay on `polling`, the default — a
signed delivery is accepted and persisted under either preset):

```bash
t3 <overlay> github_app status   # confirm `last_verified_delivery=never` before testing
```

Trigger a real event from GitHub's App settings → Advanced → "Redeliver" on any
past delivery, or push/open a PR on an installed repo. Then:

```bash
t3 <overlay> github_app status   # `last_verified_delivery` should now be recent
```

A delivery that fails signature verification, is oversized, or arrives with no
secret configured is recorded to `WebhookRejection` and surfaced in `status`'s
"recent rejections" section — inspect it before assuming delivery is broken at
GitHub's end.

## Preset selection

```bash
t3 <overlay> github_app set-preset webhook    # fails closed with no recent verified delivery
t3 <overlay> github_app set-preset polling    # always allowed — the fallback needs no verification
```

`set-preset webhook` refuses (exit 2) unless an active, un-suspended
installation has a verified delivery within the last 24 hours. This is
deliberate: cutting over to a transport nobody has proven reachable would
silently stop discovering GitHub state.

## Polling fallback

`polling` is not a deletion-bound migration shim — it is a permanently
supported transport (local development, or any installation without a
reachable webhook endpoint). It walks each active installation's confirmed
repos for `pull_request` and `issues` updates every tick, via the
`github_polling` scanner, gated by `github_transport_preset`. It shares the
identical normalize/persist path the webhook view uses
(`teatree.core.github_app.webhook_normalize` +
`teatree.core.views._webhook_persistence.persist_incoming_event`), keyed by the
same transport-independent identity
(`teatree.core.github_app.event_identity`), so a webhook delivery and a poll
discovery of the same logical update collapse onto one row rather than
double-ingesting during the validation window before cutover.

## Rollback

Any step is reversible with no data loss:

- **Preset**: `t3 <overlay> github_app set-preset polling` — always allowed, takes effect
  next tick.
- **Repo access**: repos removed from `active_repositories`/`pending_repositories`
  never re-add themselves; re-confirm with `confirm-repos` when ready.
- **App suspension**: suspending the App from GitHub's UI sets
  `GitHubAppInstallation.suspended_at`; the poller skips a suspended
  installation and the webhook view still records — but does not act on —
  deliveries from it.
- **Full removal**: uninstalling the App (or deleting it) leaves the
  `GitHubAppInstallation`/`GitHubPollCursor`/`WebhookRejection` rows in place
  for audit; delete them manually if desired. No teatree code path depends on
  their absence.
