# signal-cli, pinned by version and tarball checksum.
#
# The standard tarball plus a JRE rather than the `-Linux-native` GraalVM build:
# native images are smaller, but the HTTP daemon and `link` flow are the path
# Hermes documents and the one with the least surprise. 0.14.10 is compiled for
# class file version 69 (Java 25), so a 17 JRE fails at startup.
FROM eclipse-temurin:25-jre-noble

ARG SIGNAL_CLI_VERSION=0.14.10
ARG SIGNAL_CLI_SHA256=706a57d9faae7525cf732936efdabf210146e3c83f9e8ae7e4e62f5946121c33

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The tarball is 120 MiB and a moved tag cannot change what runs: the checksum is
# checked before anything is unpacked.
RUN curl -fsSL -o /tmp/signal-cli.tar.gz \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    && echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/signal-cli.tar.gz -C /opt \
    && ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli \
    && rm -f /tmp/signal-cli.tar.gz

# Unprivileged, and its data directory is the account credential: it lives on a
# PVC and is never baked into the image.
RUN groupadd -g 10001 signal \
    && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin signal \
    && mkdir -p /var/lib/signal-cli \
    && chown -R 10001:10001 /var/lib/signal-cli

USER 10001
ENV HOME=/home/signal
VOLUME ["/var/lib/signal-cli"]

ENTRYPOINT ["signal-cli", "--config", "/var/lib/signal-cli"]
CMD ["--version"]
