# TeaTree

TeaTree provides the `t3` CLI and a Claude plugin (skills, agents, hooks).

The skills, agents, and hooks are delivered through the Claude plugin, which
`t3 setup` registers into `~/.claude/plugins/`. This APM package exists to
declare TeaTree's companion dependencies (superpowers and the `ac-*` skills
listed in `apm.yml`) so `apm install -g souliane/teatree` pulls them in.

Install:

```sh
uv tool install git+https://github.com/souliane/teatree
apm install -g souliane/teatree
t3 setup
```
