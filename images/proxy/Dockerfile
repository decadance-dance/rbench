# The allowlist proxy of a trial (design.md D3). The allowlist and squid.conf are part of the
# image, so the image digest pins the policy that a trial ran under.
# limit: the squid package is not version-pinned; the built image's digest pins what ran.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

RUN apt-get update \
    && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf
COPY allowlist /etc/squid/allowlist
COPY healthcheck /usr/local/bin/healthcheck
RUN squid -k parse

EXPOSE 3128
HEALTHCHECK --start-period=30s --start-interval=1s --interval=30s --timeout=2s CMD ["healthcheck"]
CMD ["squid", "-N", "-d", "1"]
