# CI failure 2: DarkFac CI / pr-validation (ubuntu-latest)

[DarkFac CI / pr-validation (ubuntu-latest)]
Errors: Process completed with exit code 1.
---
GUARD VIOLATION: Agent attempted to modify protected governance files!
==================================================
  [BLOCKED] core/harness/affected.py

These files can ONLY be modified by a direct human commit.
##[error]Process completed with exit code 1.
pr-validation (windows-latest)	Enforce base-owned governance policy	﻿2026-10-08T16:18:58.2301985Z ##[group]Run python ../trusted/core/orchestrator/guard.py 2f0c0698e4ca7d0f93f7b467fc78352eedb837f9
^[[36;1mpython ../trusted/core/orchestrator/guard.py 2f0c0698e4ca7d0f93f7b467fc78352eedb837f9^[[0m
shell: C:\Program Files\PowerShell\7\pwsh.EXE -command ". '{0}'"
##[endgroup]
==================================================
GUARD VIOLATION: Agent attempted to modify protected governance files!
==================================================
  [BLOCKED] core/harness/affected.py

These files can ONLY be modified by a direct human commit.
##[error]Process completed with exit code 1.

[DarkFac CI / trusted-pr-policy]
Errors: Process completed with exit code 1.
---
trusted-pr-policy	Enforce base-owned governance policy	﻿2026-10-08T16:18:48.0335071Z ##[group]Run python ../trusted/core/orchestrator/guard.py 2f0c0698e4ca7d0f93f7b467fc78352eedb837f9
^[[36;1mpython ../trusted/core/orchestrator/guard.py 2f0c0698e4ca7d0f93f7b467fc78352eedb837f9^[[0m
shell: /usr/bin/bash -e {0}
##[endgroup]
==================================================
GUARD VIOLATION: Agent attempted to modify protected governance files!
==================================================
  [BLOCKED] core/harness/affected.py

These files can ONLY be modified by a direct human commit.
##[error]Process completed with exit code 1.
