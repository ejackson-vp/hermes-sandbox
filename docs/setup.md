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

Hermes has no Service and no Ingress. It talks out, nothing talks in.

## Prerequisites

1. The sandbox stack is healthy: `curl -s http://api.sandbox-system.svc.cluster.local:3000/readyz`
   from inside the cluster returns `{"ok":true}`. If the API is down, Hermes still
   starts, and every command fails rather than running on the host.
2. The sandbox repo's `allow-api` NetworkPolicy admits the `hermes` namespace. The
   homelab repo patches this in `apps/sandbox/release.yaml`; without it Hermes can
   resolve the Service and cannot connect.
3. `hermes-secrets` exists with `graphn-api-key` and, once Signal is linked, the
   Signal settings.

## Configuration

Non-secret settings live in `deploy/configmap.yaml`: the provider backend, the
API URL, tier and image. Secrets come from `hermes-secrets`.

Hermes reads `config.yaml` once at startup. A ConfigMap change does **not** roll
the pod (Reloader is scoped to `sandbox-system`), so after editing it:

```bash
kubectl -n hermes rollout restart deploy/hermes
```

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
