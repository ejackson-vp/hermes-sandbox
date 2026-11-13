# Hermes Agent with the x402 sandbox provider.
#
# The base is the upstream, digest-agnostic tag published for v0.23.1. Pin the
# tag in Flux (deploy/release.yaml) and the digest comes from the registry, so a
# moved tag cannot change what runs.
#
# Only one Python dependency is missing from the upstream image: `eth-account`,
# used to sign the mock EIP-3009 authorisation at session create. PyNaCl (the
# sealed envelope) and httpx are already present, so this stays a thin layer.
FROM docker.io/nousresearch/hermes-agent:v0.23.1

USER root

# The upstream venv is built by uv and ships no pip. ensurepip is available, and
# the venv is root-writable at build time (read-only only for the runtime user).
RUN /opt/hermes/.venv/bin/python -m ensurepip --upgrade >/dev/null \
    && /opt/hermes/.venv/bin/python -m pip install --no-cache-dir "eth-account>=0.13,<1" \
    && /opt/hermes/.venv/bin/python -c "from eth_account import Account; print('eth-account ready')"

# The provider package goes into site-packages so `import hermes_sandbox` works
# from any plugin context, and the plugin directory stays a thin shim.
COPY pyproject.toml /tmp/build/pyproject.toml
COPY hermes_sandbox /tmp/build/hermes_sandbox
RUN /opt/hermes/.venv/bin/python -m pip install --no-cache-dir /tmp/build \
    && rm -rf /tmp/build \
    && /opt/hermes/.venv/bin/python -c "import hermes_sandbox; print('provider installed', hermes_sandbox.__all__[0])"

# Visible to the plugin loader, which scans child directories of the plugin root.
COPY deploy/plugin/__init__.py /opt/hermes/plugins/sandbox/__init__.py
COPY plugin.yaml /opt/hermes/plugins/sandbox/plugin.yaml

# Match the rest of /opt/hermes: readable and traversable, not writable.
RUN chmod -R a+rX,go-w /opt/hermes/plugins/sandbox

USER hermes

# Temporary diagnostic sidecar used to capture the exact provider request body.
COPY deploy/proxy/capture_proxy.py /opt/hermes/tools/capture_proxy.py
