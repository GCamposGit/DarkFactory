# Dark Factory E2E Game Test Plan

## Objective

Prove, with executable evidence, that DarkFac can take one compact product idea through research, model routing, multi-tier generation, deterministic implementation, visual synthesis, validation, adversarial review, self-learning, and operator handoff in one autonomous run.

The test product is **Echo Garden**, a dependency-free six-turn puzzle. Each move changes three energy channels and the previous move returns as a rotated echo on the next turn. The player wins by bringing the channels into resonance before the turn budget expires.

## Happy path

1. Prime repository and learning-ledger context.
2. Reuse the current daily benchmark and route the coding and image tasks.
3. Load the two research ledgers for procedural game design and open-source references.
4. Ask three coding tiers for bounded, structured contributions:
   - local fast: UI naming/configuration fixture;
   - OpenRouter balanced: a candidate winning sequence;
   - OpenRouter frontier: adversarial review of the rules and candidate trace.
5. Validate every response with Pydantic contracts and the pure game engine. A response is not accepted because a model says it is correct.
6. Generate three visual artifacts:
   - deterministic local procedural title card;
   - fast/economy OpenRouter image;
   - high-complexity OpenRouter image.
7. Export a self-contained HTML game and a JSON evidence report.
8. Run the five-level test ladder, local and cloud adversarial review, self-learning benchmark, meta-evolution report, and Learning Pack.

## Non-goals

- Local demonstration projects and their tests are outside this plan.
- No game engine dependency, package manager, database, production deploy, or user account.
- No execution of arbitrary model-generated source code.
- No claim that a simulated race is a live model invocation.
- No silent local fallback counted as a successful cloud-image test.
- No secrets or raw API keys in logs, manifests, fixtures, or generated HTML.

## Test matrix

| Gate | Workflow | Driver | Pass condition |
| --- | --- | --- | --- |
| G0 | Governance and clean scope | git/library | Protected files unchanged; local demonstrations remain outside the candidate |
| G1 | Prime + daily benchmark | CLI | Current ledger loaded once; at least one routed model per tier |
| G2 | Research + code scout | CLI/files | Both ledgers persist; canonical research has sources; scout records permissive repositories |
| G3 | Game domain | library | Deterministic replay, invariants, win/loss, invalid move and turn-budget tests pass |
| G4 | Three coding tiers | local HTTP + OpenRouter HTTP | Three real responses, exact provider/model evidence, schema valid, balanced sequence wins, frontier verdict approves |
| G5 | Three visual tiers | local library + OpenRouter Images API | Three non-empty files; dimensions readable; requested cloud provider/model recorded; strict cloud mode forbids fallback |
| G6 | Browser artifact | generated HTML | No remote runtime dependency; manifest and rules embedded; file opens from the workspace |
| G7 | Static/unit/integration | harness/pytest | Syntax and all shared tests pass with non-zero discovery |
| G8 | E2E headless | CLI | Build and verify commands emit deterministic pass markers and evidence report |
| G9 | Holdout | protected harness | Hidden contract checks pass when the holdout suite is present |
| G10 | Adversarial review | Ollama + different-family cloud | Local pre-review and cloud review return structured verdicts; deterministic guard passes |
| G11 | Self-learning | CLI/library | Checkpoint, RCA, benchmark and prune complete; regressions remain executable |
| G12 | Learning Pack | CLI/files | JSON, Markdown, HTML and Anki artifacts are generated |

## Model and image tiers

The live run resolves availability before dispatch. The initial coding choices are `qwen-code-fast:latest` locally, `mistralai/mistral-nemo` for balanced cloud work, and `openai/gpt-6-astra` as frontier reviewer. The image path uses `darkfac-vector-v1` locally, `google/gemini-3.1-flash-lite-image` for a fast cloud draft, and `openai/gpt-image-2` for the final cloud asset.

Every live result records requested model, returned model, provider, latency, token/cost data when supplied, validation outcome, and artifact hash. Missing credentials, a provider mismatch, an empty response, an invalid schema, or a local fallback in a strict cloud gate is a failure rather than a skip.

## Ticket slicing

1. **Repair baseline regression** — make the benchmark-domain API test derive its expectation from the canonical catalog. Validate with the targeted pytest.
2. **Headless game domain** — add typed game contracts, deterministic engine, and unit/property tests. Validate with `pytest tests/test_echo_garden.py -v`.
3. **One-shot multi-tier builder** — add response parsing, contribution validation, HTML export, live CLI, and mocked integration tests. Validate with the builder tests.
4. **Real OpenRouter image path** — implement the dedicated Images API response contract, strict-provider mode, and mocked image tests. Validate with visual tests.
5. **Integrated harness and evidence** — add the live harness config, execute the live run, run the shared harness, adversarial review, self-learning benchmark, and Learning Pack.

## Required commands

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v
python core/harness/runner.py --config harness.live.config.json
python -m core.learning.cli benchmark
python core/orchestrator/guard.py HEAD
```

Success requires `[HARNESS_PASS]`, at least one executed check, a verified Echo Garden win trace, and real provider evidence for every gate labelled live.
