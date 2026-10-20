# DarkFac initial profile and threat inventory

Version 1 / USR-226 / 2026-10-11. DarkFac-only, no campaign authorization.
Owner of pilot policy: project owner. Operational consumers: factory development,
review and delivery stages. The profile is a contract, not an activated runtime policy.

## Evidence and unknowns by module

| Module/asset | Evidence and boundary | Threat/control/test or justified gap |
|---|---|---|
| core.security policy metadata | models.py/profiles.py; caller -> pure helper | absent/stale/foreign policy allows effect -> fail-closed helper -> synthetic missing/revision/scope tests |
| core.enterprise residency/ACL | core/enterprise/models.py; enterprise project config -> profile | foreign project policy -> scope validator -> mutation/scope test; actual provider residency requires enterprise consumers, not this helper |
| core.projects project identity | core/projects/models.py ProjectDescriptor; project registry -> caller | unknown project -> literal DarkFac plus comparison -> invalid contract/project mismatch tests; registry consumer integration pending SQ-13 |
| core.line prompts and review inputs | core/line/stage_review.py; local repository -> inference provider | unauthorized disclosure -> required ACL/residency/target/export checks; no implemented cross-system enforcement here, unknown consumer integration USR-174 |
| core.git delivery metadata | core/git/autonomy.py; candidate -> remote GitHub/production | unauthorized publication/deploy -> explicit profile effect permission modeled; caller integration absent SQ-13, actual target authorization unknown |
| core.harness test evidence | core/harness/runner.py; candidate -> worker/gate evidence | stale candidate/test claims -> existing immutable candidate official gate; this pilot has no worker or runner policy integration |

Applicable threats in structured caller inventories must carry both control_refs
and test_refs. N/A requires a concrete reason and cannot claim control/test coverage.
References describe intended coverage; the models do not execute tests or verify
remote sources. Unknown inventory/classification is recorded, never interpreted
as public or safe. The inventory is initial, not evidence that all module assets
or all threats have been discovered. Full portfolio inventories remain SQ-31+.

Initial exposure: local pure Python API. Data strategy: synthetic test inputs;
internal policy descriptions, confidential/restricted real inputs excluded from
fixtures. Residency: reuse EnterpriseProjectConfig; no provider is selected or
activated. Controls outside helper: actual ACL, secrets handling, inference
destination, trust crossing permissions, budgets and campaign limits remain
in existing consumers/future integration; none is granted by this document.

Function/module/project manual and dictionary: docs/manuals/security_profiles.md.
Before consultation pin revision, verify trusted hash and ACL as described there;
this document is not its own trusted integrity authority.
