# Signal

Hermes talks to Signal through `signal-cli` in HTTP mode: JSON-RPC out, SSE in.
The adapter handles Note to Self, attachments, chunking at the 8,000 character
limit, reactions and typing indicators.

## Note to Self, with your own number

Run `signal-cli` as a **linked device on your own number**, not a second number.
Messages you send yourself arrive as `syncMessage.sentMessage` envelopes; the
adapter processes them as normal inbound messages and filters its own replies by
sent-timestamp, so there is no reply loop.

Requirement: `SIGNAL_ACCOUNT` must be your own E.164 number.

## Linking (the one manual step)

Account linking is a QR scan. It cannot be automated and the credentials must
never be committed.

**Run it detached inside the container.** This is the part that wastes an
afternoon if you get it wrong. Do not hold the link open under `kubectl exec`:
when that session is torn down the provisioning socket dies mid-handshake and
the phone reports the unhelpful `Linking device failed / Invalid response from
service`. The daemon was healthy every time.

```bash
POD=$(kubectl -n hermes get pod -l app=hermes -o jsonpath='{.items[0].metadata.name}')
kubectl -n hermes exec -c signal-cli "$POD" -- sh -c '
  nohup signal-cli --verbose --config /var/lib/signal-cli \
    link -n HermesAgent > /var/lib/signal-cli/link.log 2>&1 &
  echo started'
```

The log's first line is the `sgnl://linkdevice?...` URI. signal-cli only draws a
QR when stdout is a terminal, so render one from the URI:

```bash
kubectl -n hermes exec -c signal-cli "$POD" -- \
  grep -o 'sgnl://[^ ]*' /var/lib/signal-cli/link.log | head -1
python3 -c "import qrcode,sys; qr=qrcode.QRCode(border=3,box_size=18); \
  qr.add_data(sys.argv[1]); qr.make(fit=True); \
  qr.make_image().save('signal-link-qr.png')" '<the uri>'
```

Scan with Signal -> Settings -> Linked Devices -> Link New Device.

Two things that bite:

- **Signal rate-limits link attempts** to roughly one a minute. Repeated failures
  produce a cooldown message on the phone, and the URI expires while you wait.
  Generate the QR, then scan it immediately, once.
- **`pkill -f "signal-cli .* link"` inside the container kills your own `sh -c`**,
  because the pattern matches that shell's argv. Use `pgrep -x java` and kill by
  pid if you need to clear a stale attempt.

After a successful scan the log ends with `Associated with: +<number>`.

Verify:

```bash
kubectl -n hermes exec -c signal-cli "$POD" -- \
  signal-cli --config /var/lib/signal-cli listAccounts
kubectl -n hermes exec -c signal-cli "$POD" -- curl -s localhost:8080/api/v1/check
```

signal-cli keeps its session data in `~/.local/share/signal-cli` on
`PVC/hermes-data`. That directory **is** a credential: it can send as you. It is
covered by the backup job, which means the backup volume holds it too.

## Configuration

In `hermes-secrets`:

```yaml
stringData:
  signal-account: "+44..."          # your number, E.164
  signal-allowed-users: "+44..."    # only these senders are answered
```

Hermes reads them as `SIGNAL_ACCOUNT` and `SIGNAL_ALLOWED_USERS`. Groups stay
disabled unless `SIGNAL_GROUP_ALLOWED_USERS` is set, which is the default and the
right one for a personal assistant.

Do not set `SIGNAL_ALLOW_ALL_USERS`. Without an allowlist, unknown senders get a
DM pairing code that must be approved with `hermes pairing approve signal <code>`.

## Running the daemon

The daemon must stay up and be reachable at `SIGNAL_HTTP_URL`
(`http://127.0.0.1:8080` when it shares a pod with Hermes). It needs Java 17+.

Either a second container in the same pod, or a separate Deployment with a
Service. Nothing is exposed outside the cluster: there is no Ingress and the
NetworkPolicy has no ingress rule.

Restart behaviour: Hermes reconnects with exponential backoff (2s to 60s) and
pings the daemon after 120s of silence. If the daemon restarts, Hermes
re-establishes the SSE stream on its own; if the *link* is broken, Signal drops
the device and you must scan a new QR.

## Background job notifications

Cron jobs deliver to `SIGNAL_HOME_CHANNEL`, which should be your own number for
Note to Self. A long task started with `terminal(background=true)` reports its
completion through the gateway, so the interactive turn does not need to stay
open.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| no reply to Note to Self | `SIGNAL_ACCOUNT` is not your number, or the device link expired |
| replies to nobody | `SIGNAL_ALLOWED_USERS` is empty or missing your number |
| duplicate replies | two signal-cli instances linked to the same number |
| "cannot reach signal-cli" | daemon down, or `SIGNAL_HTTP_URL` wrong |
| reconnect loop | `kubectl logs` for `signal-cli` errors; check Java version |
