# teatree headless deployment image — one image, three roles (init/worker/admin)
# selected by $TEATREE_ROLE. Extends the dev/Dockerfile.test toolchain and adds
# gh. The teatree source is NOT baked: it is cloned onto a volume at runtime and
# installed editable by the init role, so the image is just the toolchain.
FROM ubuntu:24.04

# Toolchain layer — ca-certificates, curl, git, jq, sqlite3, direnv, node 24, the
# claude CLI (as in dev/Dockerfile.test) plus gh, plus pass/gnupg so the box can
# route the Anthropic credential through a per-account pass store
# (anthropic_oauth_pass_paths) instead of a single env token.
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl direnv git gnupg jq pass sqlite3 && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y -qq --no-install-recommends nodejs && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends gh && \
    npm install -g @anthropic-ai/claude-code && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN useradd -m -d /home/teatree -s /bin/bash teatree

# Pre-create the volume mount points owned by teatree so a fresh named volume
# inherits teatree ownership (Docker seeds an empty volume from the image dir).
# /opt/teatree/uv carries the runtime-installed teatree python + venv + shims so
# all three services see the same t3.
RUN mkdir -p \
        /home/teatree/teatree \
        /home/teatree/.local/share/teatree \
        /home/teatree/.local/share/teatree-worktrees \
        /home/teatree/workspace/t3-workspaces \
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
