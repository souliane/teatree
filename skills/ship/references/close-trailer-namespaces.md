# Per-Namespace Close-Trailer Gate (`ban_close_trailers_on_namespaces`)

Some namespaces drive their issue lifecycle through a separate workflow and forbid the platform's auto-close behaviour entirely. `ban_close_trailers_on_namespaces` is a DB-home setting — configure those namespaces in the `ConfigSetting` store:

```bash
t3 <overlay> config_setting set ban_close_trailers_on_namespaces '["my-group/*"]'
```

When the target PR/MR repo matches one of these fnmatch patterns and the body still carries a `Closes|Fixes|Resolves` trailer (the `part of` and full-URL variants too), `ShipExecutor.build_pr_spec` silently strips those lines before opening the PR — the publish proceeds, the issue does not auto-close on merge. Default empty list keeps legacy behaviour. This is the user-scoped sibling of the overlay-scoped `forbid_close_keywords` gate (#1012) which refuses the publish entirely.
