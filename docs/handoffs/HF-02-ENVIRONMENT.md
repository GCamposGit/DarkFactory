# HF-02 laboratory environment

Status: `waiting_access` for PostgreSQL; local dependency/API preparation is complete.

This document is the reproducible environment handoff for HF-02-01. It does not select a
runtime and does not modify the production runtime, Hub, PostgreSQL service, or local demonstration projects.

## Frozen local environment

- Ticket: `HF-02-01`
- Base SHA: `1f931fcccc2f4a54cde1f0ba09825094dbe429af`
- Python: CPython 3.12.10 on Windows 11 AMD64
- DBOS: `2.31.1`, latest stable non-pre-release observed in PyPI metadata on 2026-09-09
- PostgreSQL driver: `psycopg==3.3.5` and `psycopg-binary==3.3.5`
- Process metrics: `psutil==7.2.2`
- Exact transitive lock: [`requirements.lock.txt`](../../spikes/runtime_choice/requirements.lock.txt)
- Machine-readable metadata: [`versions.json`](../../spikes/runtime_choice/versions.json)

The lock was resolved in a dedicated disposable venv, not in the default project
environment. DBOS is deliberately absent from `requirements.txt`; the default launcher
must remain independent of the experiment.

## Local validation

The following checks passed in the dedicated venv:

```text
python -m pip check
No broken requirements found.

import dbos, psycopg, psutil
DBOS, DBOS.launch, DBOS.workflow, DBOS.step, DBOS.recv and DBOS.cancel_workflow imported
and their public signatures matched the HF-02 adapter mapping.
```

The local machine has no `docker` or `psql` executable and no PostgreSQL connection
variable exposed. Therefore no database was created and no `SELECT 1` was attempted.

## Required disposable database

An owner or authorized administrator must provision a logical database dedicated to this
experiment. The target contract is:

- database name/alias starts with `darkfac_hf02_`; this handoff uses `darkfac_hf02_lab`;
- role is not superuser and has no `CREATEDB` or `CREATEROLE` privilege;
- role can access only the experiment database;
- connection secret is supplied out-of-band through `DARKFAC_HF02_DATABASE_URL`;
- no DSN, host password, or secret value is stored in this repository.

The following smoke is the only command needed after the secret is supplied. It prints
only the server-reported database and user, never the DSN:

```powershell
$env:DARKFAC_HF02_DATABASE_URL = '<secret-manager-injected-value>'
python -c "import os, psycopg; c=psycopg.connect(os.environ['DARKFAC_HF02_DATABASE_URL']); print('database=' + c.info.dbname + ' user=' + c.info.user); c.execute('SELECT 1'); print('select_1=pass'); c.close()"
```

Before running it, verify the target identity and database name against the authorized
provisioning record. Do not reuse an application database, a superuser, or a production
credential. If the secret or route is unavailable, retain `waiting_access` and proceed
with HF-02-02/03/04; HF-02-05 and HF-02-07 remain blocked.

## Access handoff / probe

The missing external dependency is concrete:

1. Provide the secret reference for `DARKFAC_HF02_DATABASE_URL` through the approved
   secret channel, without posting its value in chat or committing it.
2. Provide the authorized database alias and role name for `darkfac_hf02_lab`.
3. Run the smoke above from the intended laboratory network, recording sanitized
   `database`, `user`, `select_1`, timestamp, and operator/environment reference.
4. Replace the `waiting_access` fields in `versions.json` with `ready` only after the
   connection and privilege checks pass.

No firewall, DNS, TLS, Docker, PostgreSQL, or VPS mutation was attempted by this ticket.
