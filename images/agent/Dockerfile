# The agent image of a trial (design.md D3): one per engine version and CLI versions, not per task.
# The build context is this directory alone, so nothing from the checkout, no task data and no
# operator file can reach a layer. Credentials (#40), the scrubbed repository (#39), managed
# configuration and shims (#38) are added later; none of them belongs in a layer built here.
# limit: Debian packages are not version-pinned; the ledger pins the built image by its digest.
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

# ca-certificates: the slim base has no CA bundle and both CLIs fail TLS without one (#20).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
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

USER node
WORKDIR /home/node
