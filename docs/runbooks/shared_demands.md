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

Before the first rollout, the deployment agent compares the current live Hub
ticket IDs and statuses with `origin/main` and keeps a snapshot of any newer
runtime rows. It must resolve any difference before replacing the old Hub
container because its pre-migration ledger was inside the container image.
After rollout, verify that a ticket created through DarkHub appears to the
worker and that its status change appears back in DarkHub. The backup daemon
includes this volume in its `.factory` snapshot and restore drill.

If either service cannot mount the shared volume, keep that service stopped and
investigate its Compose volume name and file ownership. Do not initialize a
second independent ledger or copy an older image seed over the live file.
