# The agent image of a trial (design.md D3): one per engine version and CLI versions, not per task.
# The build context is this directory alone, so nothing from the checkout, no task data and no
# operator file can reach a layer. It holds the hermetic policy (design D5): each CLI's managed
# configuration, the shims first on PATH, their collector and Claude's SessionStart hook.
# Credentials (#40) and the scrubbed repository (#39) are added per trial; neither belongs here.
# limit: Debian packages are not version-pinned; the ledger pins the built image by its digest.
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

# ca-certificates: the slim base has no CA bundle and both CLIs fail TLS without one (#20).
# python3: the shims, the collector (SO_PEERCRED, which Node lacks) and the hook (design D5).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 \
    && rm -rf /var/lib/apt/lists/*

# The exact CLI versions the engine declares; an empty one is not installed. rbench checks the
# versions before the build and reads them back from the built image.
ARG CLAUDE_VERSION=
ARG CODEX_VERSION=
RUN set -eu; pkgs=""; \
    if [ -n "$CLAUDE_VERSION" ]; then pkgs="$pkgs @anthropic-ai/claude-code@$CLAUDE_VERSION"; fi; \
    if [ -n "$CODEX_VERSION" ]; then pkgs="$pkgs @openai/codex@$CODEX_VERSION"; fi; \
    test -n "$pkgs"; \
    npm install -g --no-fund --no-audit $pkgs; \
    npm cache clean --force

# The gh stub (design D4): answers PR lookups from the trial's frozen PR, sends nothing anywhere.
COPY --chmod=755 gh /usr/local/bin/gh

# The managed configuration: the policy for every CLI process, a shim's child or not (design D5).
# The directories exist first: COPY --chmod would give the ones it creates the files' mode, and
# Claude Code then runs without its managed settings (EACCES), only warning on stderr.
RUN install -d -m 0755 /etc/claude-code /etc/codex /opt/rbench/lib /opt/rbench/bin
COPY --chmod=0644 managed-settings.json /etc/claude-code/managed-settings.json
COPY --chmod=0644 requirements.toml /etc/codex/requirements.toml
COPY --chmod=0755 lib/ /opt/rbench/lib/
# The collector runs as its own user, so trial processes (user node) cannot change its log.
# /etc/rbench gets the trial's shim settings, written by root when the trial starts.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin rbench-collector \
    && install -d -o rbench-collector -m 0755 /run/rbench \
    && install -d -o rbench-collector -m 0700 /var/log/rbench \
    && install -d -m 0755 /etc/rbench \
    && ln -s ../lib/shim.py /opt/rbench/bin/claude \
    && ln -s ../lib/shim.py /opt/rbench/bin/codex

# One configuration home per CLI for the whole trial, empty until the trial writes the engine's
# settings. They are the CLIs' default paths too, so a process that drops the variables still
# lands in them.
ENV PATH=/opt/rbench/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/node/.claude \
    CODEX_HOME=/home/node/.codex
USER node
WORKDIR /home/node
RUN mkdir -m 0700 /home/node/.claude /home/node/.codex
