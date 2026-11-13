# Hermes on the homelab

A persistent personal agent that runs every shell command inside a leased x402
sandbox microVM. The agent process is trusted; the commands it runs are not.

```
Signal  ->  signal-cli (in-cluster)  ->  Hermes gateway  ->  GraphN
                                              |
                                              | sealed exec envelope
                                              v
                                    sandbox API (ClusterIP)
                                              |
                                    controller -> relay -> Kata guest
```

## What is deployed

| Object | Namespace | Purpose |
| --- | --- | --- |
| `Deployment/hermes` | `hermes` | the gateway, one replica |
| `PVC/hermes-data` | `hermes` | conversation history, memory, cron, session map |
| `PVC/hermes-backups` | `hermes` | nightly backups |
| `CronJob/hermes-backup` | `hermes` | 03:17 UTC, 7 day retention |
| `NetworkPolicy/default-deny`, `allow-hermes` | `hermes` | DNS, sandbox API on 3000, HTTPS egress |
| `Secret/hermes-secrets` | `hermes` | SOPS-encrypted, in the homelab repo |
| `Secret/hermes-payer-key` | `hermes` | optional stable payer key, SOPS-encrypted, own Secret |

Hermes has no Service and no Ingress. It talks out, nothing talks in.

## Prerequisites

1. The sandbox stack is healthy: `curl -s http://api.sandbox-system.svc.cluster.local:3000/readyz`
   from inside the cluster returns `{"ok":true}`. If the API is down, Hermes still
   starts, and every command fails rather than running on the host.
2. The sandbox repo's `allow-api` NetworkPolicy admits the `hermes` namespace. The
   homelab repo patches this in `apps/sandbox/release.yaml`; without it Hermes can
   resolve the Service and cannot connect.
3. `hermes-secrets` exists with `graphn-api-key` and, once Signal is linked, the
   Signal settings. Optional: `hermes-payer-key` with `payer-key`, if the sandbox
   allowlists a stable payer address (see below).

## Configuration

Non-secret settings live in `deploy/configmap.yaml`: the provider backend, the
API URL, tier and image. Secrets come from `hermes-secrets`, and the optional
payer key from `hermes-payer-key`.

Hermes reads `config.yaml` once at startup. A ConfigMap change does **not** roll
the pod (Reloader is scoped to `sandbox-system`), so after editing it:

```bash
kubectl -n hermes rollout restart deploy/hermes
```

## Allowlisted payer (optional, needed before mainnet)

Hermes normally pays each session from a wallet it creates on the spot, so the
paying address changes every time and an allowlist cannot match it. Setting
`HERMES_PAYER_KEY` fixes the address. In mock mode the payment carries no value
and this changes nothing economically, but the stable address is what the
sandbox operator needs before mainnet mode is enabled.

1. Generate a key and read the address it controls:

   ```bash
   /opt/hermes/.venv/bin/python -c \
     "from eth_account import Account; a = Account.create(); print(a.key.hex()); print(a.address)"
   ```

2. Put the key in its own Secret, never the ConfigMap:

   ```bash
   kubectl -n hermes create secret generic hermes-payer-key \
     --from-literal=payer-key=0x<the 64 hex characters>
   ```

   In the homelab this is SOPS-encrypted next to `hermes-secrets`, so Flux owns it
   and the value is never in git in the clear.

3. `deploy/deployment.yaml` already reads `payer-key` from that Secret into
   `HERMES_PAYER_KEY`, with `optional: true` so the pod starts without it. Restart
   to pick the variable up:

   ```bash
   kubectl -n hermes rollout restart deploy/hermes
   ```

4. Read the address back from the running pod. This prints the address and never
   the key:

   ```bash
   kubectl -n hermes exec deploy/hermes -- \
     /opt/hermes/.venv/bin/python -m hermes_sandbox.payer_address
   # <0x and 40 hex characters, EIP-55 checksummed>
   ```

   The provider's doctor row `sandbox payer` shows the same address, or
   `ephemeral per session` when the variable is unset. A malformed key is a
   `sandbox config` failure at startup, not a failure at the first create.

5. Give the address to the sandbox operator. It must be on the sandbox payer
   allowlist before mainnet mode is switched on.

Rotate in this order: generate the new key, get its address allowlisted, then
write the new value to the Secret and restart. Replacing the key first means
creates arrive from an address the allowlist has never seen, so they are charged
or refused.

### Residual risk

The key sits in the pod that runs Hermes, and Hermes is the host that runs
model-authored commands. Code execution in that pod, or a prompt injection that
reads the environment, gets the key and can mint payments from an allowlisted
address. The allowlist is a payment bypass, not an authentication factor.

So the entry it grants must be cheap to abuse: a low tier ceiling (`short`, or
`standard` with a capped duration) and a low concurrency cap. One allowlisted
address should buy a few small sandboxes, not a fleet.

## Verifying a deployment

```bash
kubectl -n hermes get pods
kubectl -n hermes logs deploy/hermes --tail=50

# The plugin registered as a terminal backend:
kubectl -n hermes exec deploy/hermes -- /opt/hermes/bin/hermes --help 2>/dev/null | grep -i sandbox

# The provider can see the API and the pinned image:
kubectl -n hermes exec deploy/hermes -- /opt/hermes/bin/hermes doctor
```

`hermes doctor` does **not** show these rows inside a container: its terminal
check forces the backend to `local` before consulting plugins, so a plugin
backend is never probed. Verify the provider directly instead:

```bash
kubectl -n hermes exec deploy/hermes -- /opt/hermes/.venv/bin/python -c \
  "from hermes_cli.plugins_activation import activate_plugin_now; \
   activate_plugin_now('sandbox', in_process=True); \
   import agent.terminal_env_registry as r; print(r.plugin_backend_names())"
# expect: ['sandbox']
```

The provider's own `doctor_checks()` (config, API reachability, image offered)
are still correct and are what a non-container install shows.

## Troubleshooting

### `400 the model rejected the request` / `the request exceeds this model's context window`

GraphN advertises a very large window (Hermes detects 1,000,000 tokens) and
Hermes derives its output budget from that number. GraphN then validates
`prompt + max_tokens <= window`, so a ~20k prompt plus a ~1M budget is rejected
and the first turn 400s.

Measured: the ceiling for `deepseek-v4.1-flash` sits between 800k and 1.6M
prompt tokens with a small budget, so 1M is roughly the real window. The fix is to
stop asking for the whole window as output:

```yaml
providers:
  graphn:
    base_url: https://model.graphn.ai/v1
    key_env: GRAPHN_API_KEY
    context_length: 131072   # entry-level; backs every model on this route
```

`context_length` must be on the **provider entry**, not under `model`.
`model.context_length` is scoped to the configured default route and did not
take here; the startup log kept reporting `1000000 tokens (detected)`.

### Every turn fails with `400 the model rejected the request`

GraphN rejects `reasoning_effort: "medium"` with that generic message. Hermes
sends `medium` by default. `none`, `low` and `high` are all accepted; only
`medium` fails, for both `deepseek-v4.1-flash` and `qwen3-80b`.

```yaml
agent:
  reasoning_effort: "high"
```

How this was pinned down, because the error names nothing: a supervised sidecar
captured the outbound body, then the captured body was replayed against GraphN
with one field removed at a time.

| Body | Result |
| --- | --- |
| verbatim capture | 400 |
| without `reasoning_effort` | 200 |
| without `stream_options` | 400 |
| without `tools` | 400 |
| `reasoning_effort` = none / low / high | 200 |
| `reasoning_effort` = medium | 400 |

Curl-ing GraphN directly will not reproduce it if you omit `reasoning_effort`:
the default chat request works, which is what makes this expensive to find.

### `no home channel`

`SIGNAL_HOME_CHANNEL` is the delivery target for cron jobs and proactive
messages. Without it those have nowhere to send. It belongs in the secret next to
`signal-account`, set to your own number so they land in Note to Self.

## The one manual step: Signal linking

Account linking cannot be automated; it is a QR scan against your phone. Run this
in the pod and scan with Signal → Settings → Linked Devices:

```bash
kubectl -n hermes exec -it deploy/hermes -- \
  signal-cli link -n "HermesAgent"
```

Then set `SIGNAL_ACCOUNT` to your own number. With a linked device on your own
number, Signal's Note to Self messages reach Hermes and replies land in the same
conversation, with no second phone number.

Never commit signal-cli's session data. It lives on `PVC/hermes-data` and is a
credential.

## Upgrades

1. Bump the base tag in `Dockerfile` and the image tag in
   `apps/hermes/release.yaml`.
2. Push; CI builds and pushes the image.
3. `flux reconcile kustomization hermes --with-source`.

The base image tag is the version pin. The digest comes from the registry, and
`deploy/pvc.yaml` is version-independent, so an upgrade never touches state.

## Recovery

See [backup.md](backup.md).

## Known limits

- One replica. SQLite on a ReadWriteOnce volume, so scale-out would corrupt state.
  A node drain is a restart, not a failover.
- Local-path volumes are node-pinned and unencrypted. The PV cannot follow the pod
  to another node.
- Sandbox leases are capped at 30 minutes by default and 8 hours with suspend.
  A task longer than one lease rolls over to a new VM: the logical task continues,
  the process does not.
- `supports_streams` is off, so Bot Desktop and `computer_use` cannot follow the
  terminal into the guest. They are refused rather than silently running on the
  host.
