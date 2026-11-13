# hermes-sandbox

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal-environment
provider that runs every shell command, file operation and code execution inside a
leased sandbox microVM from the [x402 sandbox](https://github.com/ejackson-vp/sandbox)
service.

Hermes keeps the conversation, memory and scheduling. The sandbox keeps the
untrusted part: agent-generated commands never run on the host that holds the
keys.

```
Signal -> Hermes gateway -> GraphN
                |
                | sealed exec envelope (X25519 + XSalsa20-Poly1305)
                v
        sandbox API -> controller -> relay -> Kata guest
```

## What it is not

- Not a fork of Hermes. It is a plugin plus a thin image layer.
- Not a new orchestration layer. Background tasks and scheduling are Hermes'
  own cron and `terminal(background=true)`.
- Not a fallback. If the sandbox API is unreachable, commands fail. There is no
  code path that runs an agent command on the host.

## Layout

```
hermes_sandbox/
  provider.py     TerminalEnvironmentProvider: flags, registration, doctor checks
  environment.py  BaseEnvironment subclass: _run_bash -> _ThreadedProcessHandle
  client.py       HTTP client: create, exec, cancel, files, destroy
  payment.py      mock x402 EIP-712 + EIP-191 client commitment
  crypto.py       sealed box to the guest key
  canonical.py    canonical JSON, byte-identical to the TypeScript SDK
deploy/           namespace, PVCs, Deployment, NetworkPolicy, backup CronJob
docs/             setup, Signal, backup
```

## Why the provider subclasses `BaseEnvironment`

Hermes' documented plugin example shows a duck-typed `execute()`. The built-in
remote backends (Modal, Daytona, Vercel) instead subclass `BaseEnvironment` and
implement `_run_bash`, which inherits the timeout enforcement, interrupt
handling, output draining, stdin transport and foreground fencing that core
already got right. This provider takes the same seam.

Two contract details differ from the published docs and are worth knowing if you
are porting something:

- the tool result key is `returncode`, not `exit_code`;
- the session state after provisioning is `READY`, not `ACTIVE`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_API_URL` | `http://api.sandbox-system.svc.cluster.local:3000` | sandbox API |
| `SANDBOX_TIER` | `standard` | `short`, `standard`, `extended`, `long`, `custom` |
| `SANDBOX_IMAGE` | `guest` | validated against `GET /v1/images`; unknown values fail |
| `SANDBOX_MAX_COMMAND_SECONDS` | `300` | guest watchdog cap, mirrored to fail early |
| `SANDBOX_MIN_REMAINING_SECONDS` | `60` | roll over before the lease gets thin |
| `SANDBOX_CARRY_WORKSPACE` | `true` | archive and re-upload the workspace on rollover |
| `SANDBOX_ENV_ALLOWLIST` | empty | the only host variables ever sent to the guest |
| `HERMES_PAYER_KEY` | unset | 32-byte hex private key; makes the paying address stable so the sandbox can allowlist it |

The host environment is never forwarded wholesale, and `strip_env_keys` removes
`GRAPHN_API_KEY`, `OPENAI_API_KEY`, `CONTROLLER_ADMIN_TOKEN`, `CAPABILITY_SECRET`,
`HERMES_PAYER_KEY` and every `SIGNAL_*` name from any subprocess the agent spawns.

### Allowlisted payer

Set `HERMES_PAYER_KEY` and every session create is paid from the same address, so
a sandbox operator can allowlist it and serve those sessions without payment.
Unset, each create mints a new wallet, which no allowlist can match.

```bash
# generate: prints the key, then the address it controls
python -c "from eth_account import Account; a = Account.create(); print(a.key.hex(), a.address)"
# print the address for an existing key, and nothing else
HERMES_PAYER_KEY=<hex> python -m hermes_sandbox.payer_address
```

The printer emits one line: `0x` plus 40 hex characters, EIP-55 checksummed.
That is the value to hand to the sandbox operator, and it must be on the
allowlist before mainnet mode is enabled. A malformed key fails config
validation at startup, not the first payment.

The key is a credential sitting on the host that runs untrusted agent commands,
so treat the allowlist entry it grants as low trust: a low tier ceiling and a
low concurrency cap, not a general-purpose bypass. `strip_env_keys` removes the
variable from every subprocess the agent spawns, and `SANDBOX_ENV_ALLOWLIST`
refuses to forward it to the guest.

Deployment, rotation and the residual risk are in
[docs/setup.md](docs/setup.md#allowlisted-payer-optional-needed-before-mainnet).

## Tests

```bash
make test-unit                        # offline; needs HERMES_SRC for backend tests
make test-live SANDBOX_API_URL=...    # against a real API
```

`HERMES_SRC` points at a Hermes checkout of the same version the image ships. The
backend tests import the real `BaseEnvironment`, so they exercise the actual
seam rather than a mock of it.

The live tests prove the parts that cannot be inferred from source: the mock x402
payment payload, the client commitment signature, and the sealed envelope round
trip. They also assert that a command's hostname is the sandbox pod and not the
host.

## Deployment

`deploy/` is a plain kustomize overlay, reconciled by Flux in the homelab. See
[docs/setup.md](docs/setup.md) for prerequisites, verification and upgrades, and
[docs/backup.md](docs/backup.md) for recovery.

## Known limits

- One lease is 30 minutes by default, 8 hours with suspend. Longer work rolls
  over to a new VM: the task continues, the process does not.
- **No process persistence across commands.** The guest reaps a command's process
  tree when the operation returns, so `nohup ... &` does not survive to the next
  `exec`. Measured: a backgrounded `sleep 45` is gone two seconds later, and
  `service.start` is an HTTP-server primitive rather than a job runner. Anything
  long-running must fit in one command (`SANDBOX_MAX_COMMAND_SECONDS`, 300 by
  default) and be scheduled by the gateway, which keeps the turn alive and
  reports back. Hermes' `terminal(background=true)` therefore does not give you a
  detached job here, and the docs here say so instead of implying otherwise.
- 8 MiB per file or archive means a large workspace is not carried on rollover,
  and the rollover log says so rather than silently dropping files.
- `supports_streams` is off: Bot Desktop, `computer_use` and the browser cannot
  follow the terminal into the guest.
- One execution image per deployment. The API has no image field, so the provider
  validates a name rather than selecting from a catalog.
