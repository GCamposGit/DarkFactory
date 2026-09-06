# Dark Factory E2E — Echo Garden

Date: 2026-09-05  
Status: **PASSED with two explicit environment limitations**

## Outcome

Echo Garden is a dependency-free deterministic browser puzzle produced through a one-shot, three-tier model pipeline. A verified path (`weave`, `ground`, `weave`) reaches exact resonance `[4, 4, 4]` on turn 3. Model responses are parsed as typed inert data; the domain engine, not the LLM, establishes correctness.

## Live provider evidence

| Work | Tier | Provider/model | Validation | Cost USD |
| --- | --- | --- | --- | ---: |
| UI copy | local fast | Ollama `qwen-code-fast:latest` | Pydantic contract | 0.000000 |
| Strategy | balanced cloud | OpenRouter `mistralai/mistral-nemo` | deterministic replay | 0.000005 |
| Trace review | frontier cloud | OpenRouter `openai/gpt-6-astra` | authoritative recomputation contract | 0.013010 |
| Signal map | local procedural | `darkfac-vector-v1` | decoded raster + SHA-256 | 0.000000 |
| Concept draft | fast cloud | OpenRouter `google/gemini-3.1-flash-lite-image` | decoded raster + SHA-256 | 0.03361925 |
| Hero | frontier cloud | OpenRouter `openai/gpt-image-2` | decoded raster + SHA-256 | 0.129805 |

Total recorded cost: **$0.176439**.

## Gates executed

- Architecture/PRD/non-goals and E2E gates G0–G12: `docs/DARK_FACTORY_E2E_TEST_PLAN.md`.
- Prime intelligence and context refresh.
- Daily model benchmark: current, 314 models scanned.
- Model routing at architecture, coding, testing, research, review, content and visual stages.
- Topic research: 3 arXiv records in `.factory/research/20260905_dark_factory_game_mechanics/`.
- Code reuse scout: 3 permissive/tested candidates in `.factory/research/20260905_dark_factory_game_reuse/`.
- Live three-tier text and three-tier visual build.
- Headless replay, asset hash verification and portable HTML verification.
- Speculative racing and tournament workflow. The race is now truthfully marked `synthetic_simulation` / `model_inference_executed=false`; real inference evidence comes from the live builder.
- Continuous learning benchmark: 4/4 scenarios passed, policy pruning executed, RCAs recorded.
- Anti-slop lint: plan score 2.9; grounded release notes score 0.0.
- Learning Pack generated in Markdown, HTML, JSON and Anki TSV.
- Adversarial local review with `qwen-code-deep:latest`; its valid atomic-publication finding was fixed and regression-tested.
- Governance guard, syntax compilation, full unit/integration suite and diff whitespace check.

## Deterministic validation evidence

```text
python core/harness/runner.py --quick
[HARNESS_PASS]
132 passed

python -m pytest tests -v --ignore=tests/test_canaletto.py
132 passed

python core/orchestrator/guard.py HEAD
[GUARD PASS]

python -m core.game.cli verify --output-dir .factory/e2e_game
[E2E_PASS] echo_garden_artifact_verification
models_verified=3 assets_verified=3 winning_turns=3 final_status=won
```

The exhaustive engine test evaluates all `3^6 = 729` six-move sequences and asserts energy bounds for every transition.

## Defects discovered and fixed by this E2E

1. Benchmark domain API test hard-coded an obsolete domain count; changed to the canonical metadata set.
2. Learning Pack explicit file scope still consulted global git diff; explicit scope now isolates analysis.
3. OpenRouter visual adapter used the legacy chat path; moved to `/api/v1/images`, base64 decoding, size/type validation and atomic file replacement.
4. Strict cloud visual requests could silently fall back locally; strict mode now fails closed.
5. Model prompt/output sanitation rejected harmless comparison/arrow text and accepted pipe-joined pseudo-enums; contracts and regressions were tightened.
6. Frontier review lacked authoritative game rules and trace evidence; prompt now contains the full transition contract and recomputable before/delta/after data.
7. Offline coding race admitted `sdxl-turbo`; modality/domain filtering now excludes media-only models and execution provenance is explicit.
8. Game manifest and HTML were written sequentially; HTML is atomically replaced first, manifest last as commit marker, and the verifier rejects split-brain generations.
9. Procedural release notes fabricated changes; they now contain only supplied facts and a no-additional-claims scope note.

## Explicit limitations

- The host policy blocked sending private repository source to DeepSeek/OpenRouter for the cloud adversarial review. The review stayed local; this is not recorded as cloud approval.
- The Codex image viewer was unavailable because the Windows sandbox helper failed. Raster decoding, dimensions, byte limits and SHA-256 checks passed, but human visual QA was not performed inside the tool.

Canaletto remained outside the shared harness and was not modified.
