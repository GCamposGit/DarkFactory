# AI Account Monitor — Implementation Report

## Outcome

DarkHub now exposes account quota status and project-wide model call counters through headless domain services, REST endpoints, CLI and a dashboard panel.

## Account validation snapshot

Captured on 2026-09-05:

- OpenAI/Codex: connected; official app-server returned 89% used in the 5-hour window and 39% used in the weekly window.
- xAI/Grok: connected session confirmed; consumer-plan percentage is not exposed by the installed CLI.
- Google/Gemini: connected Antigravity session confirmed; percentage remains unknown unless a structured exhaustion event or sanitized snapshot is available.
- Ollama: local cluster connected.
- Unconfigured providers remain visible as `disconnected`, never as request failures.

## Delivered contracts

- Provider adapter registry spanning frontier, Chinese, gateway and local providers.
- Exact `null` semantics for unavailable percentages.
- Concurrent partial-success probes with cache.
- Atomic and interprocess-locked JSON ledger.
- Idempotent `invocation_id` for harness retries.
- Aggregation by provider, model, tier, harness and modality.
- Fail-open instrumentation for local, tier-2, text and image execution paths.
- Authenticated opt-in HTTP event ingestion; local CLI remains available.
- Responsive Hub cards and model table with escaped provider data.

## Validation evidence

- Feature tests: 8 passed.
- Repository harness: 135 passed; `[HARNESS_PASS]`.
- Protected-file guard: passed.
- Documentation anti-slop audit: score 0.0, pristine.
- Offline speculative race: passed, no cloud inference/cost.
- Local adversarial review exposed the cross-process lock and API authentication risks; both were addressed and regression-tested.

## Deferred external gate

The Grok 4.6 read-only review was blocked before transmission because it would send repository code to an external account. No code was sent. It can be run after explicit user authorization.

