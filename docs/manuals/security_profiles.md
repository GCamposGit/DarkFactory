# DarkFac security profiles: project, module and function manual

Version 1, USR-226, 2026-10-11. Scope: `core.security`, DarkFac Wave 0.
The source revision and this manual are updated together in each affected PR.

This module performs no I/O and launches no scans, inference, scheduler or campaign.
`evaluate_sensitive_effect` models one profile permission. Real executors do not
yet call it. Mandatory executor consumption belongs to SQ-13 / USR-174; document
query version/hash/ACL enforcement across the portfolio belongs to SQ-37.
An allowed result is not proof of target authorization, ACL, enterprise residency
or legal compliance. Consumers must check those separately before any effect.

## Data strategy and dictionary

Only synthetic policy fixtures are used in tests. Profile metadata is internal;
actual credentials, private prompts and personal data must not be stored here.
Unknown classifications remain unknown and deny effect evaluation.

| Name | Meaning/type | Unit and contract |
|---|---|---|
| schema_version | schema identifier, literal integer 1 | dimensionless; unknown schema and boolean/float/string coercion rejected |
| profile_id | stable lowercase identifier, string | no unit; nonempty pattern `[a-z0-9][a-z0-9_.-]*` |
| revision / expected_revision | positive integer profile revision / caller pinned revision | dimensionless; booleans rejected; exact match required |
| project_id | existing `ProjectDescriptor.project_id` identity | pilot accepts only `darkfac`; no alternate project registry |
| maximum_classification / classification | maximum accepted / requested DataClassification | ordered `public < internal < confidential < restricted`; unknown rejected |
| allowed_effects / effect | frozenset of SensitiveEffect / requested member | network, credentials, write, publication, deploy, inference; default empty |
| enterprise | existing EnterpriseProjectConfig or None | same project required; model reused without weakening residency/ACL |
| allowed / reason | EffectDecision boolean / reason enumeration | modeling verdict; no execution grant |
| asset_id / boundary_id / threat_id | inventory identifiers | unique assets/boundaries per module; threat unique per boundary |
| consumers | tuple of nonblank consuming module names | at least one; provenance is caller supplied, not verified by model |
| evidence / unknowns | tuples of nonblank source references / unresolved questions | either required; sources are references, never fetched or trusted automatically |
| control_refs / test_refs | tuples of nonblank control / executable-test references | both required for applicable threat; actual test execution is separate evidence |
| not_applicable_reason | nonblank rationale or None | mutually exclusive with control/test references |
| module, description, source, destination, abuse, impact | inventory descriptions | text; descriptions are not authority or resolved evidence |
| assets | tuple of ThreatAsset | at least one; identifiers unique within module |
| boundaries | tuple of ThreatBoundary | default empty; unique identifiers; referenced assets must be registered |
| threats | tuple of ThreatCase per boundary | at least one; threat identifiers unique within boundary |

No function persists input, output or evidence; no source reference is fetched.
Source references must originate from a repository/ACL-authorized caller;
validation of reference text is not verification of access or source authenticity.

Classes: public = already approved public material; internal = factory plans and
operational metadata; confidential = private customer/code/research inputs;
restricted = credentials, personal/security evidence requiring restricted ACL.
These examples are criteria, not automatic content classification.

## Function manuals

`SecurityProfile.validate_schema_version(cls, value: object) -> int` and
`ThreatModel.validate_schema_version(cls, value: object) -> int`: before-field
validators; the dictionary schema_version row defines the input. Return exactly
integer 1 or raise ValueError; reject True, False, 1.0 and "1" without coercion.
Tests: schema-version rejection matrix and exact-integer acceptance.

`SecurityProfile.validate_enterprise_scope(self) -> SecurityProfile`: model
validator, returns validated self or ValueError on cross-project enterprise
policy. Input dictionary is the profile rows above; no I/O. Tested by
`test_reuses_enterprise_config_and_checks_scope_after_mutation` and invalid-contract tests.

`evaluate_sensitive_effect(profile, *, project_id, expected_revision,
classification, effect) -> EffectDecision`: pure helper. Inputs/output meanings
are in the dictionary. None returns profile_missing; unvalidated/mutated policy
is revalidated; mismatched project/revision, unknown class/effect, excessive
sensitivity and absent effect permission deny. No exception is an allow path.
Positive and negative examples are executable in `tests/test_security_profiles.py`.

`ThreatCase.validate_coverage(self) -> ThreatCase`: returns self or ValueError;
requires controls plus tests or an exclusive justified N/A. Input fields abuse,
impact and coverage rows above. Tests: uncovered/ambiguous and justified-N/A cases.

`ThreatAsset.require_evidence_or_unknown(self) -> ThreatAsset`: returns self or
ValueError; absence of evidence must be explicit; classification None requires
unknowns. Tested by unknown-asset and blank-evidence cases.

`ThreatModel.validate_inventory(self) -> ThreatModel`: returns self or ValueError;
unique inventory identities, registered boundary asset references and evidence
or unknowns required. Round-trip/reference-integrity tests exercise the contract.

## Consultation contract

Before each human/AI consultation, retrieve this manual from the authorized
repository revision; compare its version and source revision with the candidate;
verify content SHA-256 against a trusted manifest and requester repository/file
ACL. Missing manifest, stale revision, mismatched hash or unavailable ACL means
the consultation is unverified and must not authorize effects. This is a required
consumer procedure; this pilot does not implement a global document gateway.
No owner secret or portal configuration is needed for pure synthetic tests.

Validation: `python -m pytest tests/test_security_profiles.py -q`; official gate:
`python core/harness/runner.py --quick` on immutable isolated candidate. A focused
PASS alone is not delivery acceptance.

## DarkHub coverage (Step V-4)

The pure security package is recorded as internal in hub/coverage.json: no runtime
state, endpoint or owner operation is introduced. The owner risk/evidence projection
remains future SQ-28, reusing enterprise governance DH-09. This waiver does not claim
a security screen or mandatory executor integration. Run python scripts/hub_coverage.py
before delivery; record its observed coverage counts in the ticket report.
