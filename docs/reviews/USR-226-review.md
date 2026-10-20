# USR-226 independent review (Anthropic family, implementation by Codex)

Verdict: APPROVE (no code fixes required; minor test-gap suggestions below).

## Acceptance check
1. Strict contracts: PASS. SecurityProfile/EffectDecision/Threat* use extra="forbid", frozen=True;
   schema_version and revision reject bool/float/str coercion; EnterpriseProjectConfig is reused
   (core/security/models.py:8,42), not duplicated, with a project-scope validator (models.py:52-57).
   Four classifications are enum members (models.py:11-17).
2. Helper: PASS. core/security/profiles.py denies None (profiles.py:92), invalid/model_construct/mutated
   nested input via full revalidation (95-97), project and revision mismatch (98-101, bool rejected),
   unknown class/effect (102-109, exact enum match: no case folding or stripping), classification
   ordering (110-112) and default-empty allowed_effects (models.py:41). No path returns allowed=True
   except explicit permission. Docs state no real executor enforcement (manual lines 6-10).
3. Docs: PASS. Threat model has assets/boundaries/consumers/unknowns; manual has dictionary,
   function manuals and a version/hash/ACL consultation contract.
4. Scope: PASS (3 core files, 1 test, 2 docs, coverage.json entry). 
5. No network/export: PASS (grep for http/socket/requests/urllib in core/security is empty).

## Findings (all low, non-blocking)
- L1 tests/test_security_profiles.py:253-263: no test for case/whitespace variants ("WRITE", " write",
  "Internal"). Behaviour is correct (denied as *_unknown) but unpinned; suggest adding a parametrized case.
- L2 tests: no test that effect/classification as None or a list yields *_unknown, nor that a non-profile
  object (dict) yields profile_invalid. Code handles these (profiles.py:96,104,109).
- L3 models.py:42: frozen profile holds a mutable EnterpriseProjectConfig (frozen only top-level). Mitigated
  by helper revalidation and covered by test (line 292-297); direct readers of profile.enterprise are not.
- L4 core/security has no __init__.py (core/enterprise has one). Works as a namespace package, tests pass;
  out of the declared file scope, so not changed. Consider adding in a follow-up if packaging requires it.
- L5 profiles.py:95: model_dump of a model_construct profile emits a pydantic serializer UserWarning (seen in
  pytest output); harmless, result still profile_invalid or revalidated.
- L6 Helper ignores enterprise.enabled / residency / allowed_providers; documented as consumer responsibility.

## Fixes applied
None.

## Residual risks
Helper is advisory until SQ-13/USR-174 wire executors; a caller that skips it is not blocked. Threat model
references (control_refs/test_refs) are unverified strings.

## Test result
python -m pytest tests/test_security_profiles.py tests/test_hub_coverage.py -q -p no:cacheprovider -n0: 51 passed, 1 warning.
