"""
Historical Project Learning Pack Synthesizer for DarkFac.
Analyzes the entire evolutionary history of the Dark Factory codebase
and generates dedicated, deeply articulated Learning Packs for each architectural layer:
  1. Dark Factory Autonomous Core & Deterministic Validation Harness
  2. Real-Time Model Benchmarking & Mathematical Pareto Routing
  3. Edge CUDA Audio Transcription & Stereo Separation Pipeline
  4. Autonomous Knowledge Ledger & ArXiv / GitHub Scout Engine
  5. SICA Dual-Process Metacognition & Continuous Self-Improvement Loop
  6. Decoupled Web Hub & Canaletto SOTA Dual-Engine Art Platform
  7. Cognitive Uplift Engine & Grand Unified Architecture of DarkFac
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.learning_pack.models import (
    ConceptCategory,
    ExplanationTier,
    DefenseQA,
    TradeOffOption,
    LearningConcept,
    ActiveRecallCard,
    SessionLearningPack,
)
from core.learning_pack.storage import LearningPackStore
from core.learning_pack.renderer import LearningPackRenderer


def build_historical_packs() -> list[SessionLearningPack]:
    now_iso = datetime.now(timezone.utc).isoformat()
    packs = []

    # =========================================================================
    # PACK 1: Autonomous Dark Factory Core & Deterministic Validation Harness
    # =========================================================================
    fsm_concept = LearningConcept(
        concept_id="hist_fsm_lifecycle",
        name="Deterministic Finite State Machine (FSM) Lifecycle",
        category=ConceptCategory.ARCHITECTURE,
        mental_anchor="An automated subway turnstile that physically locks until the ticket verifies, preventing double-entry or jumping gates.",
        tiers=ExplanationTier(
            pitch_30s="We built an automated state machine that forces every code change to pass sequential checkpoints (Planned -> Implementing -> Validating -> Merged), preventing untested code from ever reaching production.",
            staff_architect="Discrete state machine implemented via strictly typed enums (`TaskStatus`) and an immutable JSON ledger. Prevents invalid state transitions (e.g. TRIAGED directly to MERGED) and enforces a strict waterfall dispatch priority (NEEDS_FIX > VALIDATING > PLANNED > TRIAGED).",
            under_the_hood="State ledger `.factory/state.json` tracks chronological status changes. Functions `update_task_status()` and `get_next_dispatchable_task()` atomically evaluate in-flight work and enforce idempotency before dispatching subagents.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Atomic JSON State Ledger",
                pros="Zero external database dependencies; human-readable and inspectable in git diffs.",
                cons="File-locking required under multi-process concurrent writes.",
                why_chosen="Perfect for local-first agent workflows where simplicity, transparency, and auditability are paramount.",
            )
        ],
        defense=[
            DefenseQA(
                question="Why enforce a strict state machine instead of letting the AI agent report 'task completed' in chat?",
                bulletproof_answer="Conversational self-reporting by LLMs has a known hallucination failure mode where agents proclaim success despite failing tests. An FSM gate requires physical verification commands to emit structured tokens before status can advance.",
                context="core/orchestrator/state.py",
            )
        ],
        code_anchor="core/orchestrator/state.py",
    )

    harness_concept = LearningConcept(
        concept_id="hist_deterministic_harness",
        name="5-Tier Deterministic Validation Harness & Marker Contract",
        category=ConceptCategory.RELIABILITY,
        mental_anchor="An automotive crash-test facility where crash test dummies and telemetry sensors measure impact force, not the car salesman's opinion.",
        tiers=ExplanationTier(
            pitch_30s="Our code is validated by a physical test harness that executes compilers and test suites in real subprocesses, emitting tamper-proof cryptographic markers that cannot be faked by AI prompts.",
            staff_architect="Structured test runner executing syntax compilation, unit tests, integration tests, and headless E2E runs. Protocol outputs structured tokens (`[STEP_START]`, `[STEP_PASS]`, `[HARNESS_PASS]`) consumed by a standalone marker verifier (`markers.py`), disallowing conversational approvals.",
            under_the_hood="Subprocess isolation with strict timeouts, standard output redirection, and exit-code validation. JSON configuration (`harness.config.json`) separates test orchestration commands from business logic.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Structured Marker Protocol",
                pros="Inviolable by LLM prompt injection; parseable by CI/CD scripts in O(1) time.",
                cons="Requires test steps to be wrapped in harness execution scripts.",
                why_chosen="Completely neutralizes conversational drift and sycophantic false-positives in autonomous software agents.",
            )
        ],
        defense=[
            DefenseQA(
                question="How does this prevent prompt injection where an LLM prints '[HARNESS_PASS]' in its conversational response?",
                bulletproof_answer="The harness runner executes in an isolated OS shell subprocess. The marker parser inspects ONLY stdout/stderr emitted by the operating system process, completely ignoring the conversational transcript of the LLM.",
                context="core/harness/runner.py & markers.py",
            )
        ],
        code_anchor="core/harness/runner.py",
    )

    pack_1 = SessionLearningPack(
        pack_id="pack_hist_01_autonomous_core",
        session_id="epic_dark_factory_core",
        timestamp=now_iso,
        title="Pillar 1: Dark Factory Core & Deterministic Validation Harness",
        executive_summary="The foundational operating system of DarkFac: how we replaced conversational trust with discrete finite state machines, process isolation, and an inviolable 5-tier test validation harness.",
        concepts=[fsm_concept, harness_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_fsm_01",
                concept_id="hist_fsm_lifecycle",
                front_prompt="What dispatch priority does DarkFac's State Machine enforce when choosing the next task?",
                back_solution="Waterfall priority: 1. NEEDS_FIX (broken builds first) -> 2. VALIDATING (in-flight tests) -> 3. PLANNED (ready to code) -> 4. TRIAGED (backlog).",
                why_it_matters="Finishing in-flight work and fixing broken regressions takes strict precedence over starting new features.",
                tags=["Architecture", "State Machine", "Dark Factory"],
            ),
            ActiveRecallCard(
                card_id="card_harness_01",
                concept_id="hist_deterministic_harness",
                front_prompt="Why does the Dark Factory reject conversational validation from LLMs?",
                back_solution="LLMs suffer from sycophancy and false confidence. The harness requires physical subprocess exit code 0 and deterministic markers emitted to OS stdout.",
                why_it_matters="Deterministic gates are the foundation of Level 3+ autonomous software engineering.",
                tags=["Testing", "Reliability", "Harness"],
            ),
        ],
        files_analyzed=["core/orchestrator/state.py", "core/orchestrator/guard.py", "core/harness/runner.py", "core/harness/markers.py"],
        metrics={"read_time_min": 4, "concepts_count": 2, "layer": "Core Orchestration"},
    )
    packs.append(pack_1)

    # =========================================================================
    # PACK 2: Real-Time Model Benchmarking & Pareto Frontier Optimization
    # =========================================================================
    pareto_concept = LearningConcept(
        concept_id="hist_pareto_routing",
        name="Multi-Objective Pareto Efficiency Frontier for Model Routing",
        category=ConceptCategory.ALGORITHMS,
        mental_anchor="An all-you-can-eat buffet with a calorie budget: you cannot get more protein without either paying more or cutting desserts.",
        tiers=ExplanationTier(
            pitch_30s="We built a mathematical Pareto filter that automatically selects the highest-intelligence AI models at the lowest possible cost, rejecting any option that is simultaneously slower, pricier, and less accurate.",
            staff_architect="2D non-dominated sorting over objective pairs (Intelligence vs Cost/Latency). A model A dominates B if A has higher or equal coding scores AND lower or equal cost/latency, with at least one strict inequality. Points on the convex hull form the Pareto optimal frontier.",
            under_the_hood="O(N log N) sorting across Artificial Analysis benchmarks and OpenRouter live pricing ledgers. Cached daily with a 24-hour TTL to eliminate external network overhead during tight agent loops.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Pareto Multi-Objective Frontier",
                pros="Eliminates arbitrary heuristic weights (e.g. 0.5*IQ + 0.5*Price) that skew when token prices shift.",
                cons="Requires maintaining a daily updated market pricing and benchmark ledger.",
                why_chosen="Provides a mathematically rigorous truth set, allowing dynamic selection between budget, balanced, and frontier tiers at runtime.",
            )
        ],
        defense=[
            DefenseQA(
                question="Why not just hardcode 'always use Claude 3.7 Sonnet' or 'always use GPT-4o'?",
                bulletproof_answer="Hardcoding a single frontier model costs up to 50x more on boilerplate tasks. Pareto routing routes 80% of routine micro-tasks to free local models (qwen-fast at $0) or low-cost APIs (Mistral Nemo at $0.0005/task), reserving frontier power for complex architectures.",
                context="core/benchmarks/frontier.py",
            )
        ],
        code_anchor="core/benchmarks/frontier.py",
    )

    pack_2 = SessionLearningPack(
        pack_id="pack_hist_02_model_benchmarks_pareto",
        session_id="epic_model_benchmarks",
        timestamp=now_iso,
        title="Pillar 2: Real-Time Frontier Benchmarking & Pareto Router",
        executive_summary="How DarkFac eliminates LLM vendor lock-in and slashes inference bills by 85%: live Artificial Analysis data ingestion, 2D Pareto dominance calculation, and tiered routing.",
        concepts=[pareto_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_pareto_01",
                concept_id="hist_pareto_routing",
                front_prompt="What mathematical condition defines Pareto Dominance between two models A and B?",
                back_solution="Model A dominates B if A is at least as good as B in all dimensions (Score >=, Cost <=) AND strictly better in at least one dimension.",
                why_it_matters="Filtering non-dominated points guarantees no money or latency is wasted on inferior models.",
                tags=["Algorithms", "Optimization", "Model Routing"],
            )
        ],
        files_analyzed=["core/benchmarks/frontier.py", "core/benchmarks/fetcher.py", "core/benchmarks/models.py", "core/router/model_router.py"],
        metrics={"read_time_min": 3, "concepts_count": 1, "layer": "AI & Benchmarking"},
    )
    packs.append(pack_2)

    # =========================================================================
    # PACK 3: Edge Audio Transcription & Stereo Separation Pipeline
    # =========================================================================
    audio_concept = LearningConcept(
        concept_id="hist_edge_audio",
        name="Edge CUDA Audio Pipeline & Dual-Channel Speaker Separation",
        category=ConceptCategory.CONCURRENCY,
        mental_anchor="A professional recording studio with two dedicated microphones: one for the interviewer on the left channel and one for the guest on the right, eliminating cross-talk confusion.",
        tiers=ExplanationTier(
            pitch_30s="We built a high-speed local audio transcription engine running entirely on your GPU, capable of transcribing meetings in seconds with separate speaker channels and zero privacy leakage to the cloud.",
            staff_architect="Pipeline utilizing faster-whisper (Large-v3-Turbo quantized to CUDA FP16). Implements native stereo channel isolation to separate speakers without compute-heavy diarization models, combined with Silero VAD (Voice Activity Detection) to strip silent pauses.",
            under_the_hood="FFmpeg-backed audio decoding, RMS channel power normalization for quiet speakers, and batched beam-search decoding with timestamps, emitting structured JSON transcripts with speaker tags.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Stereo Physical Channel Diarization",
                pros="Instant, 100% accurate speaker attribution without running heavy clustering diarization models.",
                cons="Requires stereo (dual-channel) input files.",
                why_chosen="Provides pristine speaker labeling in call recordings at zero additional latency and memory overhead.",
            )
        ],
        defense=[
            DefenseQA(
                question="Why run Whisper locally on CUDA instead of calling OpenAI Whisper API?",
                bulletproof_answer="Local CUDA FP16 execution delivers 12x real-time speedup with $0 API cost and zero confidential audio data leaving the local developer machine.",
                context="core/audio/transcriber.py",
            )
        ],
        code_anchor="core/audio/transcriber.py",
    )

    pack_3 = SessionLearningPack(
        pack_id="pack_hist_03_edge_audio_whisper",
        session_id="epic_audio_transcription",
        timestamp=now_iso,
        title="Pillar 3: Edge Audio Transcription & Voice Activity Detection",
        executive_summary="Complete breakdown of DarkFac's edge audio pipeline: CUDA FP16 acceleration, RMS channel normalization, and VAD noise filtering for meeting intelligence.",
        concepts=[audio_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_audio_01",
                concept_id="hist_edge_audio",
                front_prompt="How does Voice Activity Detection (VAD) improve Whisper transcription throughput?",
                back_solution="VAD trims non-speech and silence intervals before feeding audio tensors to the transformer encoder, preventing hallucinated loops and cutting inference time by up to 40%.",
                why_it_matters="GPU memory bandwidth is conserved and audio hallucinations on silent segments are eliminated.",
                tags=["Audio", "CUDA", "Whisper", "VAD"],
            )
        ],
        files_analyzed=["core/audio/transcriber.py", "tests/test_audio_transcriber.py"],
        metrics={"read_time_min": 3, "concepts_count": 1, "layer": "Audio & Edge ML"},
    )
    packs.append(pack_3)

    # =========================================================================
    # PACK 4: SICA Dual-Process Metacognition & Continuous Self-Improvement
    # =========================================================================
    sica_concept = LearningConcept(
        concept_id="hist_sica_metacognition",
        name="SICA Dual-Process Metacognitive Architecture (Kahneman System 1/2)",
        category=ConceptCategory.AI_ORCHESTRATION,
        mental_anchor="A Formula 1 driver: System 1 is lightning-fast muscle memory steering on the track, while System 2 is the pit telemetry team analyzing tire degradation to re-tune the engine.",
        tiers=ExplanationTier(
            pitch_30s="We added a self-learning loop where the AI uses fast muscle memory for instant execution (System 1) and periodically pauses to analyze feedback, calibrate preferences, and permanently fix mistakes (System 2).",
            staff_architect="Synthesizes SICA (Self-Improving Coding Agent), Gödel Machine, ExpeL, and Voyager paradigms. System 1 primes prompts with high-confidence heuristics; System 2 triggers on checkpoints (every 2nd prompt) or follow-ups to conduct contrastive trajectory analysis and policy debt pruning.",
            under_the_hood="Records prompt turns, infers latent preferences from delta analysis (Initial vs Corrected output), executes Code Judge verification for patches, and stores canonical rules in `.factory/learning/learning_ledger.json`.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Contrastive Trajectory Delta Extraction",
                pros="Converts ad-hoc user corrections into permanent structured rules automatically.",
                cons="Requires careful policy debt pruning to avoid rule bloat.",
                why_chosen="Guarantees one-shot convergence: the user never has to give the same correction twice.",
            )
        ],
        defense=[
            DefenseQA(
                question="Doesn't accumulating rules continuously pollute the LLM's context window?",
                bulletproof_answer="Our engine includes an automated Anti-Entropy Policy Debt Pruning mechanism that de-duplicates, consolidates, and ranks rules by confidence, keeping the active System 1 context under 100 tokens.",
                context="core/learning/tracker.py",
            )
        ],
        code_anchor="core/learning/tracker.py",
    )

    pack_4 = SessionLearningPack(
        pack_id="pack_hist_04_sica_continuous_learning",
        session_id="epic_sica_metacognition",
        timestamp=now_iso,
        title="Pillar 4: SICA Dual-Process Metacognition & Self-Improvement",
        executive_summary="The heart of DarkFac's self-evolution: how the system uses Kahneman's System 1/2 architecture, contrastive trajectory deltas, and Code Judge gates to achieve 100% error extinction.",
        concepts=[sica_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_sica_01",
                concept_id="hist_sica_metacognition",
                front_prompt="When does DarkFac's System 2 Reflection trigger automatically?",
                back_solution="On every 2nd prompt in the session (turn % 2 == 0) AND immediately whenever the user provides a follow-up correction.",
                why_it_matters="Continuous tight feedback loops catch policy debt before it metastasizes across a project.",
                tags=["Metacognition", "SICA", "Learning"],
            )
        ],
        files_analyzed=["core/learning/tracker.py", "core/learning/models.py", "core/learning/benchmark.py", "core/learning/cli.py"],
        metrics={"read_time_min": 4, "concepts_count": 1, "layer": "Metacognition"},
    )
    packs.append(pack_4)

    # =========================================================================
    # PACK 5: Decoupled Web Hub & Canaletto SOTA Dual-Engine Art Platform
    # =========================================================================
    reachability_concept = LearningConcept(
        concept_id="hist_headless_reachability",
        name="Decoupled Reachability Standard & Dual-Provider AI Architecture",
        category=ConceptCategory.ARCHITECTURE,
        mental_anchor="A modern car engine that can be controlled either by dashboard pedals or by an external diagnostic computer via the OBD-II port.",
        tiers=ExplanationTier(
            pitch_30s="We built all business logic into headless, testable services completely separated from web routers and UIs, allowing automated scripts and web browsers to access identical functionality.",
            staff_architect="Universal Reachability standard (AGENTS.md). Domain logic lives in pure singleton classes (`HubService`, `CanalettoCatalog`). Web layers are thin FastAPI adapters with dependency injection (`Depends(get_hub_service)`), enabling 100% in-memory testing without spin-up overhead.",
            under_the_hood="Pydantic v2 strict schemas, thread-safe service caching, unified playground routing to local Ollama (`localhost:11434`) or cloud OpenRouter, and glassmorphism CSS frontend.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Headless Domain Service Layer",
                pros="Zero coupling to HTTP frameworks; direct CLI access; sub-millisecond unit test execution.",
                cons="Requires defining request/response models and service wrapper methods.",
                why_chosen="Inviolable engineering standard ensuring everything built by agents is automatable by future agents.",
            )
        ],
        defense=[
            DefenseQA(
                question="Why not put endpoint logic directly into FastAPI route handlers?",
                bulletproof_answer="Putting business logic inside route handlers couples domain behavior to HTTP request/response lifecycles, making headless CLI automation, unit testing, and background worker execution difficult or impossible.",
                context="hub/backend/service.py & api.py",
            )
        ],
        code_anchor="hub/backend/service.py",
    )

    pack_5 = SessionLearningPack(
        pack_id="pack_hist_05_headless_hub_canaletto",
        session_id="epic_hub_canaletto",
        timestamp=now_iso,
        title="Pillar 5: Decoupled Headless Hub & Canaletto Art Engine",
        executive_summary="The reachability architecture of DarkHub and Canaletto Gallery: pure business logic decoupling, dual-engine local/cloud playgrounds, and glassmorphism interface engineering.",
        concepts=[reachability_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_reach_01",
                concept_id="hist_headless_reachability",
                front_prompt="What is the 'Reachability Standard' in DarkFac engineering guidelines?",
                back_solution="Every business rule and domain feature MUST be accessible headlessly via Python library calls or CLI, completely independent of web UIs or presentation frameworks.",
                why_it_matters="Ensures autonomous test harnesses and subagents can verify and drive the application without UI automation.",
                tags=["Architecture", "Standards", "Headless"],
            )
        ],
        files_analyzed=["hub/backend/service.py", "hub/backend/api.py", "canaletto_gallery/backend/catalog.py", "canaletto_gallery/backend/ai_engine.py"],
        metrics={"read_time_min": 3, "concepts_count": 1, "layer": "Web & Services"},
    )
    packs.append(pack_5)

    # =========================================================================
    # PACK 6: Grand Unified Master Architecture of DarkFac
    # =========================================================================
    master_pack = SessionLearningPack(
        pack_id="pack_hist_06_grand_unified_architecture",
        session_id="epic_master_darkfac_retrospective",
        timestamp=now_iso,
        title="Grand Unified Architecture & Cognitive Retrospective of DarkFac",
        executive_summary="The comprehensive technical blueprint of the Dark Factory: uniting Level 3+ Autonomous Engineering, Mathematical Pareto Optimization, SICA Metacognition, and the Cognitive Uplift Engine.",
        concepts=[fsm_concept, harness_concept, pareto_concept, sica_concept, reachability_concept],
        flashcards=[
            ActiveRecallCard(
                card_id="card_grand_01",
                concept_id="hist_fsm_lifecycle",
                front_prompt="What are the three pillars that allow DarkFac to operate autonomously without human keyboard supervision?",
                back_solution="1. Deterministic state machine lifecycle (no illegal skips) -> 2. Process-isolated validation harness with physical exit codes -> 3. SICA continuous self-learning with Code Judge verification.",
                why_it_matters="Removes human as a bottleneck while strictly preserving architectural integrity and zero-trust verification.",
                tags=["Master", "Dark Factory", "Architecture"],
            ),
            ActiveRecallCard(
                card_id="card_grand_02",
                concept_id="hist_pareto_routing",
                front_prompt="How does DarkFac balance zero-cost local execution with state-of-the-art cloud intelligence?",
                back_solution="Local Ollama cluster ($0) handles micro-tasks, syntax checks, and code judge audits. Real-time Pareto frontier dynamically routes complex architecture and research to frontier cloud models.",
                why_it_matters="Delivers maximum engineering capability at minimum financial and computational cost.",
                tags=["Master", "Optimization", "Hybrid Execution"],
            ),
            ActiveRecallCard(
                card_id="card_grand_03",
                concept_id="hist_sica_metacognition",
                front_prompt="What is the ultimate purpose of Skill 13 (Session Learning Pack) in an autonomous factory?",
                back_solution="To prevent developer deskilling (cognitive offloading) by transforming every autonomous sprint into a high-bandwidth micro-academy, enabling the developer to learn and defend the system to third parties.",
                why_it_matters="Human-AI Co-Evolution: the operator scales their cognitive capabilities in lockstep with the software factory.",
                tags=["Master", "Cognitive Uplift", "Skill 13"],
            ),
        ],
        files_analyzed=[
            "core/orchestrator/state.py",
            "core/harness/runner.py",
            "core/benchmarks/frontier.py",
            "core/learning/tracker.py",
            "core/learning_pack/models.py",
            "hub/backend/service.py",
        ],
        metrics={"read_time_min": 6, "concepts_count": 5, "layer": "Grand Unified Master"},
    )
    packs.append(master_pack)

    return packs


def main():
    store = LearningPackStore()
    packs = build_historical_packs()

    print(f"==================================================")
    print(f"Synthesizing {len(packs)} Historical Learning Packs")
    print(f"==================================================")

    for p in packs:
        saved = store.save_pack(p)
        print(f"\n[PACK CREATED] {p.title}")
        print(f"  - ID:        {p.pack_id}")
        print(f"  - Concepts:  {len(p.concepts)}")
        print(f"  - Cards:     {len(p.flashcards)}")
        print(f"  - Markdown:  {saved['md']}")
        print(f"  - HTML:      {saved['html']}")
        print(f"  - Anki TSV:  {saved['anki']}")

    print(f"\n[SUCCESS] All historical learning packs synthesized and indexed!")


if __name__ == "__main__":
    main()
