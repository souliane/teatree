# Skill Delegation Matrix

Source: `teetree.skill_map.DEFAULT_SKILL_DELEGATION`

## Delegation

| Phase | Delegated Skills |
| --- | --- |
| `coding` | `test-driven-development`, `verification-before-completion` |
| `debugging` | `systematic-debugging`, `verification-before-completion` |
| `reviewing` | `requesting-code-review`, `verification-before-completion` |
| `shipping` | `finishing-a-development-branch`, `verification-before-completion` |
| `ticket-intake` | `writing-plans` |

## TeaTree Responsibilities Retained Locally

- Worktree lifecycle orchestration
- Task claiming, leasing, and execution routing
- Quality-gate state tracking on sessions
- Generated dashboard and documentation surfaces

## Agent Launch Fields

- `phase`
- `overlay_skill_path`
- `companion_skills`
- `delegated_skills`
