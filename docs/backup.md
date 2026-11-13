# Backup and recovery

## What is at risk

`PVC/hermes-data` holds everything the assistant remembers:

| Path | Contents | Consequence of loss |
| --- | --- | --- |
| `*.db` | conversation history, memory, run records | the assistant forgets every conversation |
| `cron/` | scheduled jobs and their definitions | reminders stop existing |
| `config.yaml` | local overrides | reconstructable from the repo |
| `skills/`, `plugins/` | installed skills and plugin state | reconstructable, re-synced on boot |
| `cache/` | disposable | none |

The sandbox workspaces are **not** part of this. They live in the guest and are
destroyed with the lease, by design. Anything worth keeping must be copied out
before the lease ends; the provider carries the workspace across a rollover but
not across a Hermes restart.

## Automatic backups

`CronJob/hermes-backup` runs at 03:17 UTC daily and writes to
`PVC/hermes-backups/<timestamp>/`:

- each `*.db` copied with `sqlite3 .backup`, which is safe while the gateway is
  writing (this is the important one);
- `state.tar.gz` with config, cron, skills and plugins;
- directories older than 7 days are deleted.

If the `sqlite3` binary is absent from the image, the job logs
`0 database(s) copied` and only the tar is taken. The tar alone is a
crash-consistent copy: a write in flight during the tar can leave a database
needing recovery. Check the log:

```bash
kubectl -n hermes logs job/$(kubectl -n hermes get jobs -o name | tail -1 | cut -d/ -f2)
```

## Manual backup before an upgrade

```bash
kubectl -n hermes create job --from=cronjob/hermes-backup hermes-backup-manual
kubectl -n hermes logs -f job/hermes-backup-manual
```

## Restore

State and image are independent, so a restore never needs a matching image.

1. Scale the agent down, so nothing is writing while files are replaced.

   ```bash
   kubectl -n hermes scale deploy/hermes --replicas=0
   kubectl -n hermes wait --for=delete pod -l app=hermes --timeout=120s
   ```

2. Move the failed state aside rather than deleting it. A half-restored state is
   usually still worth inspecting.

   ```bash
   kubectl -n hermes exec deploy/hermes -- true  # fails; use a helper pod instead
   ```

   There is no shell in a scaled-down Deployment, so use a throwaway pod that
   mounts both claims:

   ```bash
   kubectl -n hermes apply -f - <<'YAML'
   apiVersion: v1
   kind: Pod
   metadata: {name: hermes-restore, namespace: hermes}
   spec:
     restartPolicy: Never
     securityContext: {fsGroup: 10000}
     containers:
       - name: restore
         image: ghcr.io/ejackson-vp/hermes-sandbox:0.1.2
         command: ["sleep", "3600"]
         volumeMounts:
           - {name: data, mountPath: /opt/data}
           - {name: backups, mountPath: /backups}
     volumes:
       - name: data
         persistentVolumeClaim: {claimName: hermes-data}
       - name: backups
         persistentVolumeClaim: {claimName: hermes-backups}
   YAML
   ```

3. Restore the newest good backup.

   ```bash
   kubectl -n hermes exec hermes-restore -- sh -c '
     set -eu
     latest=$(ls -1d /backups/*/ | sort | tail -1)
     echo "restoring from $latest"
     mv /opt/data /opt/data.broken.$(date +%s)
     mkdir -p /opt/data
     cp -a "$latest"/. /opt/data/
     chown -R 10000:10000 /opt/data'
   ```

4. Bring it back and verify.

   ```bash
   kubectl -n hermes delete pod hermes-restore
   kubectl -n hermes scale deploy/hermes --replicas=1
   kubectl -n hermes rollout status deploy/hermes
   # then, from Signal: ask it what you talked about yesterday
   ```

## Restoring on a new cluster

The PVC is `local-path`, so it is bound to one node. On a rebuilt node:

1. Create the namespace and claims (`kubectl apply -k deploy/` creates them empty).
2. Copy the last backup from wherever it survives. The `hermes-backups` claim does
   **not** survive a node loss, so keep a copy off-box if the history matters.
   A `kubectl cp` of `/backups` to your workstation is enough:

   ```bash
   kubectl -n hermes cp hermes-restore:/backups ./hermes-backups
   ```

3. Follow the restore steps above, copying from the local directory instead.

## What is not covered

- Off-box backup. The backup claim sits on the same node as the data.
- Point-in-time recovery. One snapshot per day.
- A quiesced backup. Database files are consistent, the rest of the tree is a
  live copy.
