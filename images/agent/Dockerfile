# The agent image of a trial (design.md D3): one per engine version and CLI versions, not per task.
# The build context is this directory alone, so nothing from the checkout, no task data and no
# operator file can reach a layer. It holds the hermetic policy (design D5): each CLI's managed
# configuration, the shims first on PATH, their collector and Claude's SessionStart hook.
# Credentials (#40) and the scrubbed repository (#39) are added per trial; neither belongs here.
# limit: Debian packages are not version-pinned; the ledger pins the built image by its digest.

# rreview at the commit its declaration pins (engines/rreview.toml), built from source with its
# lock file; only the binary reaches the agent image. An empty commit builds nothing.
# limit: the Rust base is pulled for every agent image, also when rreview is not installed.
FROM rust:1.99.1-slim-bookworm@sha256:f2c004e3d2d8b547e8cf7ffbc29205385f4e71d23e53e4e383065db5e9f822cd AS rreview
ARG RREVIEW_COMMIT=
RUN set -eu; mkdir -p /out/bin; \
    if [ -n "$RREVIEW_COMMIT" ]; then \
        cargo install --locked --root /out \
            --git https://github.com/decadance-dance/rreview --rev "$RREVIEW_COMMIT" rreview; \
    fi

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

# rreview, when the engine is rreview. It refuses a Claude reviewer without zsh, the shell it runs
# Claude's commands in so that no bash login reads startup files. The zsh version apt picked is
# recorded in /opt/rbench/zsh.version, so two builds of the same pins can be told apart.
ARG RREVIEW_COMMIT=
COPY --from=rreview /out/bin/ /usr/local/bin/
RUN set -eu; if [ -n "$RREVIEW_COMMIT" ]; then \
        apt-get update && apt-get install -y --no-install-recommends zsh \
        && rm -rf /var/lib/apt/lists/* \
        && install -d -m 0755 /opt/rbench \
        && dpkg-query -W -f '${Version}\n' zsh > /opt/rbench/zsh.version; \
    fi

# The engine's Claude Code plugin at the commit its declaration pins (design D5, Plugins at the
# pin), fetched by that exact commit, without its history, into /opt/rbench/plugins/<name>, the
# name from its .claude-plugin/plugin.json; root-owned and read-only to the trial. With it comes
# mikefarah yq 4.44.3, checked by SHA-256: a plugin may read YAML config with it (review-council
# ignores its config files without it). An empty PLUGIN_COMMIT installs neither.
ARG PLUGIN_REPO=
ARG PLUGIN_COMMIT=
ARG TARGETARCH
RUN set -eu; if [ -n "$PLUGIN_COMMIT" ]; then \
        src=$(mktemp -d); \
        git -C "$src" init -q; \
        git -C "$src" fetch -q --depth 1 "$PLUGIN_REPO" "$PLUGIN_COMMIT"; \
        git -C "$src" checkout -q FETCH_HEAD; \
        test "$(git -C "$src" rev-parse HEAD)" = "$PLUGIN_COMMIT"; \
        name=$(node -p 'require(process.argv[1]).name' "$src/.claude-plugin/plugin.json"); \
        echo "$name" | grep -Eqx '[a-z0-9][a-z0-9-]*'; \
        rm -rf "$src/.git"; \
        install -d -m 0755 /opt/rbench/plugins; \
        cp -R "$src" "/opt/rbench/plugins/$name"; \
        rm -rf "$src"; \
        chown -R root:root "/opt/rbench/plugins/$name"; \
        chmod -R u+rwX,go+rX,go-w "/opt/rbench/plugins/$name"; \
        case "$TARGETARCH" in \
            arm64) sum=0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156 ;; \
            amd64) sum=a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7 ;; \
            *) echo "no pinned yq for $TARGETARCH" >&2; exit 1 ;; \
        esac; \
        curl -fsSL -o /usr/local/bin/yq \
            "https://github.com/mikefarah/yq/releases/download/v4.44.3/yq_linux_$TARGETARCH"; \
        echo "$sum  /usr/local/bin/yq" | sha256sum -c -; \
        chmod 0755 /usr/local/bin/yq; \
    fi

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
