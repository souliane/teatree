# teatree headless deployment image — one image, five roles (init/worker/admin/
# slack-listener/watchdog) selected by $TEATREE_ROLE. Extends the
# dev/Dockerfile.test toolchain and adds gh. The image is SELF-CONTAINED (#3451):
# it BAKES a pinned source@ref + the managed Python interpreter + the locked
# editable install (see the bake stage near the end), so a fresh box with only
# the image + secrets boots deterministically and offline. The runtime clone on
# the teatree_src volume stays the self-update path — the entrypoint fast-forwards
# it from origin when online and runs the baked snapshot when offline.
FROM ubuntu:24.04

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

# ============================================================
# Bake the product — source@ref + interpreter + LOCKED editable install (#3451).
# Mirrors dev/Dockerfile.test:19-34 (bake the managed Python + `uv` locked deps so
# nothing is cold-resolved from PyPI/astral at runtime), extended to also bake the
# pinned SOURCE and the editable `t3` tool. Both bake targets sit UNDER the two
# named volumes (teatree_src -> /home/teatree/teatree, teatree_uv -> /opt/teatree/uv):
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

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
