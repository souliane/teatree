# teatree headless deployment image — one image, five roles (init/worker/admin/
# slack-listener/watchdog) selected by $TEATREE_ROLE. Extends the
# dev/Dockerfile.test toolchain and adds gh. The image is SELF-CONTAINED (#3451):
# it BAKES a pinned source@ref + the managed Python interpreter + the locked
# editable install (see the bake stage near the end), so a fresh box with only
# the image + secrets boots deterministically and offline. The runtime clone on
# the teatree_src volume stays the self-update path — the entrypoint fast-forwards
# it from origin when online and runs the baked snapshot when offline.
#
# Digest-pinned to the same ubuntu:24.04 manifest dev/Dockerfile.test pins, so a
# retag of the floating tag can never silently change the deploy toolchain.
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

# Toolchain layer — ca-certificates, curl, git, jq, sqlite3, direnv, node 24, the
# claude CLI (as in dev/Dockerfile.test) plus gh, plus pass/gnupg so the box can
# route the Anthropic credential through a per-account pass store
# (anthropic_oauth_pass_paths) instead of a single env token. ttyd backs the
# admin dashboard's loopback "Debug session" terminal (#3263). The Docker CLI
# + compose plugin (CLI only, NO engine) let the watchdog role drive the host
# daemon over the mounted socket. The npm `pyright` package provides the
# `pyright-langserver` binary the pyright-lsp Claude plugin execs to give factory
# agents live Python type diagnostics while coding (`t3 setup` enables the plugin).
#
# The claude CLI pin here is set INDEPENDENTLY of the generation `claude-agent-sdk`
# bundles (which every eval/test image pins), even when the two coincide. This image
# feeds the paths that exec the GLOBAL binary — `t3 loop start`'s os.execv, the agent
# command, the ttyd web terminal, the tasks command — so it tracks a current
# known-good release and may lead the bundle, never trail it: a generation behind
# `claude-opus-5` breaks model_tiering's `frontier` tier. Both tiers are guarded by
# tests/test_claude_cli_pin.py.
# bash with `pipefail`, not the default dash `/bin/sh`. dash has no `pipefail`, so
# a `curl ... | bash` whose FETCH fails still reports only the shell's exit status
# and the `&&` chain sails on. That silently broke the box: NodeSource answered the
# setup script with HTTP 403 on one build, the repo was therefore never added,
# `apt-get install nodejs` happily installed Ubuntu's nodejs 18 — which packages
# npm SEPARATELY, so npm was absent — and the layer died 6 seconds later at
# `npm install -g` with a bare `/bin/sh: 1: npm: not found` and exit 127, naming
# neither the 403 nor the wrong node. Every fetch below is now attributable.
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl direnv git gnupg jq pass sqlite3 ttyd && \
    # Fetch the NodeSource setup script to a FILE, never straight into a shell, so
    # a truncated or error body cannot be executed as a script; and retry, so a
    # transient CDN 403/5xx costs seconds instead of the whole deploy.
    for attempt in 1 2 3 4 5; do \
        if curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh; then break; fi; \
        echo "deploy image: NodeSource setup_24.x fetch failed (attempt ${attempt}/5) - retrying" >&2; \
        if [ "$attempt" -lt 5 ]; then sleep 15; fi; \
    done && \
    if [ ! -s /tmp/nodesource_setup.sh ]; then \
        echo "deploy image: FATAL - could not fetch https://deb.nodesource.com/setup_24.x after 5 attempts. Without that repo apt installs Ubuntu's nodejs 18, which ships no npm, and this build would die later with an unattributable 'npm: not found'." >&2; \
        exit 1; \
    fi && \
    bash /tmp/nodesource_setup.sh && \
    rm -f /tmp/nodesource_setup.sh && \
    apt-get install -y -qq --no-install-recommends nodejs && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod go+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends gh docker-ce-cli docker-compose-plugin && \
    # Assert the node that landed is the NodeSource one before leaning on npm.
    # `npm: not found` from the line below is the symptom that cost a week of
    # deploys; say what it MEANS at the moment it is provable.
    if ! command -v npm >/dev/null 2>&1; then \
        echo "deploy image: FATAL - npm is absent after installing nodejs ($(node --version 2>/dev/null || echo 'node missing')). The NodeSource repo did not take effect, so this is Ubuntu's nodejs, which packages npm separately." >&2; \
        exit 1; \
    fi && \
    npm install -g @anthropic-ai/claude-code@2.1.220 pyright@1.1.411 && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN useradd -m -d /home/teatree -s /bin/bash teatree

# Pre-create the volume mount points owned by teatree so a fresh named volume
# inherits teatree ownership (Docker seeds an empty volume from the image dir).
# /opt/teatree/uv carries the runtime-installed teatree python + venv + shims so
# all three services see the same t3.
# ~/.claude/projects is pre-created (and so chowned to teatree below) BEFORE it is
# used as the projects bind-mount target: without it, Docker creates the mount's
# parent ~/.claude as ROOT at container start, so `t3 setup` cannot write
# ~/.claude/settings.json and the init container crash-loops (whole factory down).
RUN mkdir -p \
        /home/teatree/teatree \
        /home/teatree/.local/share/teatree \
        /home/teatree/.local/share/teatree-worktrees \
        /home/teatree/workspace/t3-workspaces \
        /home/teatree/.claude/projects \
        /opt/teatree/uv && \
    # clone_root() resolves clones under ~/workspace, but the runtime clone
    # lives on the teatree_src volume at ~/teatree. Provisioning discovers the
    # base clone with find_clone_path(clone_root, "souliane/teatree"), which
    # matches the ORG-PREFIXED path ~/workspace/souliane/teatree (the slug's
    # literal), NOT a bare ~/workspace/teatree — so the link must carry the
    # owner segment. Without the org-prefixed link, `workspace ticket` fails at
    # worktree provisioning ("No git clone found for souliane/teatree") and the
    # issue-implementer intake is silently disabled. This mirrors the host
    # layout (~/workspace/souliane/teatree -> the teatree clone).
    mkdir -p /home/teatree/workspace/souliane && \
    ln -s /home/teatree/teatree /home/teatree/workspace/souliane/teatree && \
    chown -R teatree:teatree /home/teatree /opt/teatree

COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

# Align the runtime user to the HOST deploy user's UID (the TEATREE_UID build
# arg). Every host bind mount is at path identity (see deploy/README.md § UID
# invariant), so the container UID MUST equal the host deploy user's UID or every
# bind mount is unwritable and init crash-loops on its first write. deploy.sh
# derives this from the host at deploy time (`id -u` of the deploy user) and
# passes it as the build arg, so the image always matches whatever box builds it.
# The default is 1001 — the live box's deploy user, and also the UID `useradd`
# below lands on unaided (Ubuntu 24.04 ships a stock `ubuntu` user already holding
# UID 1000, so the teatree user created above takes the next free id, 1001). The
# stock `ubuntu` user is removed to free UID 1000 for boxes whose deploy user is
# 1000; the teatree user is then renumbered onto TEATREE_UID (a no-op when it
# already equals 1001) and its already-populated home tree re-owned to that id.
ARG TEATREE_UID=1001
RUN userdel -r ubuntu 2>/dev/null || true; \
    usermod -u "${TEATREE_UID}" teatree && \
    groupmod -g "${TEATREE_UID}" teatree && \
    chown -R teatree:teatree /home/teatree /opt/teatree  # privacy-scan:allow

# The deploy-managed default ~/.claude/settings.json template (#3359). The init
# role generates the container's real settings.json from this (plus env overrides)
# BEFORE `t3 setup`, so the headless agent has a configurable model / permission
# mode / autoMode grants / tool-use concurrency instead of stock CLI defaults.
# Baked (not mounted) so a mountless standalone run still has the default; edit
# this committed, reviewable file to change the loop agent's configuration.
COPY deploy/claude-settings.template.json /usr/local/share/teatree/claude-settings.template.json

USER teatree
WORKDIR /home/teatree

# uv (the only baked python toolchain) — the teatree editable install and its
# managed Python land on the shared /opt/teatree/uv volume at runtime.
# Fetched to a file, retried, and size-checked for the same reason as the
# NodeSource script above: this is the second `curl | shell` in the image, and a
# CDN blip here would otherwise pipe an error page into a shell and surface as
# some later "uv: not found" that names nothing.
RUN for attempt in 1 2 3 4 5; do \
        if curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh; then break; fi; \
        echo "deploy image: uv install.sh fetch failed (attempt ${attempt}/5) - retrying" >&2; \
        if [ "$attempt" -lt 5 ]; then sleep 15; fi; \
    done && \
    if [ ! -s /tmp/uv-install.sh ]; then \
        echo "deploy image: FATAL - could not fetch https://astral.sh/uv/install.sh after 5 attempts." >&2; \
        exit 1; \
    fi && \
    sh /tmp/uv-install.sh && \
    rm -f /tmp/uv-install.sh

# No XDG_DATA_HOME: with HOME=/home/teatree the default already resolves DATA_DIR
# to ~/.local/share/teatree (the canonical DB on the teatree_data volume). Setting
# it explicitly to that same canonical value would make paths.py refuse every
# loop-provisioned worktree (CanonicalDBFromWorktreeError) and defeat the
# teatree_worktrees auto-isolation volume.
# The pass store and its GPG home are a DEDICATED credential-plane bind mount
# (compose), decoupled from the data dir at their canonical host paths — so a
# data-dir change can never orphan the provisioned credential store again (#3262
# did exactly that by moving the data dir to a bind mount while these ENV paths
# still pointed inside it). These now equal pass/gpg defaults for this HOME; kept
# explicit for grep-ability and so a mountless standalone run still resolves.
ENV PATH="/opt/teatree/uv/bin:/home/teatree/.local/bin:${PATH}" \
    UV_TOOL_DIR=/opt/teatree/uv/tools \
    UV_TOOL_BIN_DIR=/opt/teatree/uv/bin \
    UV_PYTHON_INSTALL_DIR=/opt/teatree/uv/python \
    TEATREE_CLONE_DIR=/home/teatree/teatree \
    TEATREE_REPO_URL=https://github.com/souliane/teatree.git \
    PASSWORD_STORE_DIR=/home/teatree/.password-store \
    GNUPGHOME=/home/teatree/.gnupg

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ============================================================
# Bake the product — source@ref + interpreter + LOCKED editable install (#3451).
# Kept as the FINAL build stage (after the ENTRYPOINT metadata declaration,
# which has no build-time effect): the source-ref-pinned bake is the most
# volatile layer, so putting it last maximizes cache reuse of the stable
# toolchain/ENV/ENTRYPOINT layers above.
# Mirrors dev/Dockerfile.test:19-34 (bake the managed Python + `uv` locked deps so
# nothing is cold-resolved from PyPI/astral at runtime), extended to also bake the
# pinned SOURCE and the editable `t3` tool. Both bake targets sit UNDER the two
# named volumes (teatree_src -> $TEATREE_CLONE_DIR, teatree_uv -> /opt/teatree/uv):
# Docker seeds a FRESH volume from the image content at the mount path, so a fresh
# box inherits the baked clone + Python + tool and boots offline with zero fetches.
# An already-provisioned box keeps its populated volumes (the bake is shadowed) and
# self-updates via the entrypoint's origin fast-forward — the runtime clone remains
# the in-loop `t3 update` path (epic #3445), so this bake changes only FRESH-box boot.
#
# TEATREE_SOURCE_REF pins the baked commit (default `main`; the publish workflow
# passes the exact build SHA so the image and its lockfile-keyed tag can never
# drift). The clone keeps its full `.git` so the runtime fast-forward and `t3
# update` work. Building requires network (as the apt/node/uv layers already do) —
# it is the RUNTIME that becomes deterministic and offline-capable.
ARG TEATREE_SOURCE_REF=main
RUN git clone "$TEATREE_REPO_URL" "$TEATREE_CLONE_DIR" && \
    git -C "$TEATREE_CLONE_DIR" checkout --quiet "$TEATREE_SOURCE_REF" && \
    uv python install 3.13 && \
    uv tool install --editable "${TEATREE_CLONE_DIR}[slack]" --python 3.13 && \
    uv tool install prek==0.3.13
