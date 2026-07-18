# teatree headless deployment image — one image, three roles (init/worker/admin)
# selected by $TEATREE_ROLE. Extends the dev/Dockerfile.test toolchain and adds
# gh. The teatree source is NOT baked: it is cloned onto a volume at runtime and
# installed editable by the init role, so the image is just the toolchain.
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
# daemon over the mounted socket.
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl direnv git gnupg jq pass sqlite3 ttyd && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
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
    npm install -g @anthropic-ai/claude-code && \
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

# Align the runtime user to a DETERMINISTIC UID (the TEATREE_UID build arg,
# default 1000). Every host bind mount is at path identity (see
# deploy/README.md § UID invariant), so the container UID MUST equal the host
# deploy user's UID or every bind mount is unwritable and init crash-loops on
# its first write. Ubuntu 24.04 ships a stock `ubuntu` user already holding UID
# 1000, so it is removed to free the id; the teatree user created above (at the
# next free id) is renumbered onto TEATREE_UID and its already-populated home
# tree is re-owned to the new id. A fresh box's first `adduser` is UID 1000, so
# the default lines up with no extra step; override with
# `--build-arg TEATREE_UID=<n>` when the deploy user is not 1000.
ARG TEATREE_UID=1000
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
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

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
