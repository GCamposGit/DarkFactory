# Shared demand ledger (USR-136)

DarkHub and the cloud line run as separate Dokploy Compose applications on the
same host. Both mount the named volume `darkfac-demands-v1` at
`/app/.factory/demands`. The coordinator, worker, canary, and backup daemon
mount it too. The volume holds `demands.json`, the ID counter, and the file
locks. The image ships an independent seed at
`/app/.factory_seed/demands/demands.json`.
Each service sets `DARKFAC_DEMANDS_SHARED_VOLUME=darkfac-demands-v1`; startup
fails if the expected mount is absent. The live board labels the backlog
source and the age of its last ticket change.

At startup, `DemandsStore` merges committed seed rows into the persistent
ledger under a process-wide file lock. A newer `updated_at` wins for an existing
ID; a new ID is appended. Runtime changes therefore survive image replacement,
while newly committed tickets become visible in the live backlog. A corrupt
ledger or missing seed fails startup instead of displaying an empty backlog.
Writes use the same lock and an atomic replacement, so requests from DarkHub
and the worker cannot discard one another's distinct tickets.

On the first rollout, `scripts/dokploy_redeploy.py` reads the old Hub's entire
`GET /api/demands/tickets` response before changing Dokploy. It keeps a
pending snapshot at `.factory/tmp/shared_demands_migration_pending.json`, reads
the old Hub again just before triggering its deployment, and merges the newer
rows. If either read fails, the Hub is not replaced. After the new Hub reports
the expected Git SHA and its live view reports `demands_source=shared-volume`,
the script restores rows absent from the volume or older than the snapshot
through the existing ticket API. It reads the ledger again and removes the
pending file only after all captured rows are verified. A failed deployment
or partial restore leaves the snapshot for the next run on the same host.

This snapshot is local to the deploying harness. A process restart on that
host is recoverable; a different host cannot see its pending file. Writes to
the old Hub in the final instant between the last read and container shutdown
also cannot be captured by this API-only sequence. Until an old-container
write pause and a remote migration marker/snapshot are available, treat this
first rollout as requiring operational proof and keep its PR in draft. If the
old Hub or Dokploy API returns 404/502, the deployment fails before replacing
the Hub. After rollout, verify a ticket created in DarkHub appears to the
worker and its status change appears back in DarkHub. The backup daemon
includes the shared volume in its `.factory` snapshot and restore drill.

If either service cannot mount the shared volume, keep that service stopped and
investigate its Compose volume name and file ownership. Do not initialize a
second independent ledger or copy an older image seed over the live file.
