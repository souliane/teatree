# Multi-Tenant Development

> Canonical reference for feature flags and multi-tenant considerations. Loaded by `t3:code` and `t3:review`.

---

## Feature Flags

In multi-tenant projects, changes that affect runtime behavior or display must be isolated per tenant using feature flags.

### Decision Gate (Non-Negotiable)

Before implementing any change, determine whether it needs a feature flag:

1. **Check the ticket** for tenant scope — identify the target tenant(s).
2. **Default assumption:** if the project is multi-tenant, assume a feature flag is needed unless the change is purely internal (no runtime/display impact).
3. **Ask the user** for explicit confirmation before proceeding without a flag — even for display-only changes. State your reasoning and wait for approval.

### When a Flag Is NOT Required

- Behavior already controlled by existing DB config (e.g., tenant configuration tables) with unchanged defaults.
- Pure internal refactoring with zero runtime impact.
- The user explicitly confirms no flag is needed after reviewing your reasoning.

### Flag Discipline

| Rule | Detail |
|------|--------|
| Default state | `False` (off) — no behavior change vs status quo |
| ON scope | Enable only for in-scope tenant(s) until explicitly broadened |
| OFF guarantee | Identical behavior to the pre-change state |
| Documentation | Comment above the flag definition: purpose, behavior when ON, behavior when OFF |

> **Project overlays** extend these rules with project-specific details: flag file locations, naming conventions, access methods, test-time toggle patterns. See the project overlay's workflow reference.

## Review Checklist

When reviewing code in a multi-tenant project:

1. **Feature flag present?** If the change affects runtime behavior or display and there's no flag, raise it as a finding.
2. **Tenant scope verified?** Check the ticket's tenant/customer field to confirm scope.
3. **Flag-off path tested?** The default (flag off) must behave identically to the pre-change state.
4. **Multi-portal impact?** If the project has multiple portals/apps, verify which are affected and whether each is covered.
5. **No cross-tenant leakage?** Feature-flagged behavior must not affect tenants outside the flag's scope.
