"""
Session Codebase & Git Diff Analyzer for Learning Pack Extraction.
Inspects recent repository modifications, file contents, and AST patterns
to identify technical themes, design patterns, and code anchors.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

from core.paths import project_root

_ROOT_DIR = project_root()

# Architectural pattern signatures and their metadata
PATTERN_SIGNATURES: List[Dict[str, Any]] = [
    {
        "id": "pareto_frontier",
        "name": "Pareto Efficiency Frontier Optimization",
        "category": "Algorithms & Data Structures",
        "regex": r"(Pareto|pareto_frontier|pareto_points|dominates|frontier)",
        "file_hint": r"(frontier|benchmark|router)",
        "default_metaphor": "Like an all-you-can-eat buffet with a calorie budget: you cannot get more protein without either paying more or cutting desserts.",
        "pitch_30s": "We implemented a mathematical Pareto filter that automatically selects the highest-intelligence AI models at the lowest possible cost, rejecting any option that is simultaneously slower, pricier, and less accurate.",
        "staff_architect": "Multi-objective optimization mapping models onto a 2D Pareto frontier (Intelligence vs Cost/Latency). Guarantees O(N log N) or O(N^2) non-dominated sorting so routing decisions strictly maximize cost-performance efficiency.",
        "under_the_hood": "Iterates through candidate points (x=cost, y=score). Candidate A dominates B if A.score >= B.score and A.cost <= B.cost with at least one strict inequality. Points not dominated by any other point form the convex frontier.",
        "defense_q": "Why calculate a Pareto frontier instead of a simple weighted score (e.g. 0.5*IQ + 0.5*Price)?",
        "defense_a": "Weighted sums require arbitrary weights that break when unit scales change or stakeholder priorities shift. A Pareto frontier provides the complete non-dominated truth set, allowing dynamic selection at runtime without bias.",
    },
    {
        "id": "finite_state_machine",
        "name": "Deterministic Finite State Machine (FSM) Lifecycle",
        "category": "Architecture",
        "regex": r"(class \w*Status\(.*Enum\)|update_task_status|transition_to|TaskStatus)",
        "file_hint": r"(state|orchestrator|lifecycle|fsm)",
        "default_metaphor": "Like an automated subway turnstile: you cannot enter until the ticket validates, and you cannot swipe twice without passing through.",
        "pitch_30s": "We built a rigid state machine so tasks can only advance step-by-step through strict checkpoints (planned, built, tested, reviewed), preventing premature merges or phantom bugs.",
        "staff_architect": "Deterministic discrete lifecycle transitions enforced by an immutable state ledger. Disallows illegal state jumps (e.g., TRIAGED directly to MERGED) and enforces strict priority ordering on task resumption.",
        "under_the_hood": "Enumerated states stored in atomic JSON with transition history tracking. Priority dispatcher evaluates tasks using a waterfall check: NEEDS_FIX > VALIDATING > PLANNED > TRIAGED.",
        "defense_q": "Why not allow agents to mark tasks directly as done if all checks pass?",
        "defense_a": "Allowing agents to bypass intermediate state validation creates silent failures and unreviewable side effects. The state machine ensures every state transition produces an audit trail and triggers deterministic verification.",
    },
    {
        "id": "code_judge_gate",
        "name": "Verification-Grounded Code Judge Gate",
        "category": "Reliability & Testing",
        "regex": r"(verify_patch_with_code_judge|VerificationGate|exit code|subprocess\.run\(.*test)",
        "file_hint": r"(tracker|learning|harness|gate)",
        "default_metaphor": "Like a physical crash-test rig in an automotive factory: no matter how handsome the car looks on paper, it does not ship unless it physically passes the crash barrier.",
        "pitch_30s": "We installed an automated Code Judge that physically executes test suites before accepting any AI patch or rule change, making it impossible for hallucinations to enter the codebase.",
        "staff_architect": "Deterministic execution boundary where agent-generated artifacts are tested in isolated subprocesses. Exit code 0 and non-empty assertions are required; conversational guarantees from LLMs are explicitly treated as zero-trust.",
        "under_the_hood": "Subprocess execution with strict timeouts, capturing stdout/stderr into audit ledgers. Failed test runs abort patch application and register an RCA failure event.",
        "defense_q": "Doesn't running physical test commands slow down the agent loop?",
        "defense_a": "A 2-second deterministic test run eliminates minutes of downstream debugging and rollback overhead caused by undetected semantic regressions.",
    },
    {
        "id": "headless_reachability",
        "name": "Headless Decoupling & Reachability Architecture",
        "category": "Architecture",
        "regex": r"(class HubService|APIRouter|FastAPI\(|get_hub_service)",
        "file_hint": r"(api|service|backend|cli)",
        "default_metaphor": "Like a modern car engine that can be controlled either by the dashboard pedals or by an external diagnostic computer via the OBD-II port.",
        "pitch_30s": "We decoupled the application logic completely from the user interface, meaning every single feature can be run and tested headlessly by automated scripts without opening a browser.",
        "staff_architect": "Strict separation between presentation frameworks (FastAPI/HTML/CLI) and domain service singletons (`HubService`). Ensures 100% of business logic is testable in-memory or via unit tests without mocking UI rendering.",
        "under_the_hood": "Domain services expose pure Python typed methods. Web routers act as thin HTTP adapters with dependency injection (`Depends(get_hub_service)`), returning typed Pydantic response models.",
        "defense_q": "Why not write endpoint logic directly inside route handlers to reduce boilerplate?",
        "defense_a": "Writing logic inside route handlers couples domain behavior to HTTP request/response lifecycles, making headless CLI automation, unit testing, and background workers difficult or impossible to execute without spin-up overhead.",
    },
    {
        "id": "dual_process_learning",
        "name": "Dual-Process (System 1/2) Metacognitive Learning Loop",
        "category": "AI & LLM Orchestration",
        "regex": r"(get_active_system1_context|ContinuousLearningTracker|is_checkpoint_turn|record_trajectory_contrast)",
        "file_hint": r"(learning|tracker|models)",
        "default_metaphor": "Like a racecar driver: System 1 is fast muscle memory reflex on the track, while System 2 is the pit crew and telemetry team analyzing tire wear to re-tune the engine.",
        "pitch_30s": "We added a self-learning loop where the AI uses fast muscle memory for instant execution (System 1) and periodically pauses to analyze feedback, calibrate preferences, and permanently fix mistakes (System 2).",
        "staff_architect": "Inspired by Kahneman's Dual-Process theory and SICA/ExpeL paradigms. System 1 primes context with high-confidence heuristics; System 2 triggers on checkpoints or follow-ups to conduct contrastive trajectory analysis and policy debt pruning.",
        "under_the_hood": "Cadence checks fire on `turn % 2 == 0` or upon follow-up detection. Key deltas between initial and corrected outputs are extracted and converted into canonical user preferences stored in `.factory/learning/learning_ledger.json`.",
        "defense_q": "Doesn't accumulating rules continuously pollute the LLM's context window?",
        "defense_a": "Our engine includes an automated Anti-Entropy Policy Debt Pruning mechanism that de-duplicates, consolidates, and ranks rules by confidence, keeping the active System 1 context under 100 tokens.",
    },
    {
        "id": "isolated_process_concurrency",
        "name": "Asynchronous Task Management & Non-Blocking Subprocesses",
        "category": "Concurrency & Async",
        "regex": r"(asyncio\.gather|run_in_executor|subprocess\.Popen|WaitMsBeforeAsync)",
        "file_hint": r"(api|service|runner|cli)",
        "default_metaphor": "Like an executive assistant dispatching letters to multiple couriers at once and checking their delivery status without standing idle at the mailbox.",
        "pitch_30s": "We designed the system to run heavy background tasks and health-checks asynchronously, so the main interface and agent never freeze while waiting for long-running processes.",
        "staff_architect": "Offloads blocking I/O and external network pings to thread pools via `loop.run_in_executor` or background subprocesses, maintaining sub-millisecond API response latency.",
        "under_the_hood": "FastAPI async endpoints coordinate with standard library `asyncio` event loops. Subprocess timeouts and background tasks emit deterministic completion tokens.",
        "defense_q": "Why not use multithreading directly in Python instead of async and subprocesses?",
        "defense_a": "Python's Global Interpreter Lock (GIL) limits CPU-bound multithreading, and unhandled thread crashes can corrupt process memory. Subprocess isolation and async I/O provide crash resilience and true OS-level concurrency.",
    },
    {
        "id": "anti_slop_engine",
        "name": "Deterministic Anti-AI-Slop & Cadence Variance Engine",
        "category": "AI & LLM Orchestration",
        "regex": r"(AntiSlopLinter|slop_score|cadence_monotony|scrub_slop|ContentEngine)",
        "file_hint": r"(content|anti_slop|linter|presets)",
        "default_metaphor": "Like an executive speech coach who ruthlessly cuts corporate buzzwords and forces you to vary your rhythm instead of droning in a monotone voice.",
        "pitch_30s": "We built a deterministic filter that hunts down robotic AI clichés, analyzes sentence rhythm to kill monotonic droning, and automatically polishes text into authentic, high-signal writing.",
        "staff_architect": "Multi-tier content purification combining a 50+ term multilingual lexical scanner, statistical sentence length variance analysis (burstiness metric), and automated critique-and-scrub refinement loops with deterministic 0-100 purity scoring.",
        "under_the_hood": "Regex tokens identify forbidden patterns (buzzwords, hollow superlatives, robotic openers). Sentence tokenizer measures sentence length standard deviation, penalizing low-variance text. A deterministic scrub pass substitutes clean idioms and outputs a structured SlopReport.",
        "defense_q": "Why not just use a system prompt telling the LLM not to use buzzwords?",
        "defense_a": "LLMs suffer from frequency bias and token drift; prompt instructions alone degrade on complex topics. A deterministic post-generation linter provides mathematically verifiable score gates that cannot be bypassed by model hallucinations.",
    },
    {
        "id": "visual_asset_studio",
        "name": "Local-First Procedural & Context-Aware Visual Studio",
        "category": "Architecture",
        "regex": r"(ProceduralVisualEngine|VisualStudio|prompt_synthesizer|VisualPromptSpec)",
        "file_hint": r"(visual|studio|procedural|cloud_engine)",
        "default_metaphor": "Like having an in-house graphic designer and an automated blueprint plotter right inside the compiler, turning raw technical code into high-definition graphics in milliseconds.",
        "pitch_30s": "We implemented a zero-cost local visual studio that reads technical text, extracts the core concept, and instantly renders crisp social banners, architecture diagrams, UI mockups, and app icons offline.",
        "staff_architect": "Hybrid local-first image generation combining CPU-bound procedural rendering (Pillow vector overlays, perspective grids, and typography cards) with cloud frontier models (Gemini 2.5 Flash Image, Flux, DALL-E 3) under a unified semantic coupling interface.",
        "under_the_hood": "Synthesizer parses markdown headers and keyword frequencies to infer themes and negative prompts. Procedural engine composites multi-layer RGBA buffers (gradient background, perspective grid, radial glow, card blur, text wrap) in under 100ms with zero network calls.",
        "defense_q": "Why bother with a local procedural engine instead of just calling an image API like DALL-E 3?",
        "defense_a": "Cloud image APIs cost cents per render, have 5-15 second latency, and require internet access. Our local procedural engine costs $0, renders in 60ms, operates 100% offline, and deterministically generates crisp, legible technical typography and schematics that neural diffusion models frequently scramble.",
    }
]


class CodebaseAnalyzer:
    """Extracts architectural themes and code anchors from modified files or git diff."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or _ROOT_DIR

    def get_recent_git_diff(self) -> str:
        """Retrieves staged or unstaged diff from git."""
        try:
            res = subprocess.run(
                ["git", "diff", "HEAD~1"],
                cwd=str(self.root_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
            
            # Fallback to working tree diff
            res_working = subprocess.run(
                ["git", "diff"],
                cwd=str(self.root_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            return res_working.stdout if res_working.returncode == 0 else ""
        except Exception:
            return ""

    def get_modified_files(self) -> List[str]:
        """Gets recently modified or staged files."""
        files: Set[str] = set()
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.root_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().splitlines():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        files.add(parts[1].strip())
        except Exception:
            pass

        return sorted(list(files))

    def detect_patterns(self, file_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Inspects given files (or detected modified files) against architectural signatures.
        Returns matching pattern definitions with code anchors.
        """
        explicit_file_scope = file_paths is not None
        target_files = file_paths if explicit_file_scope else self.get_modified_files()
        diff_text = "" if explicit_file_scope else self.get_recent_git_diff()

        detected: List[Dict[str, Any]] = []
        matched_ids: Set[str] = set()

        # 1. Scan specific target files if available
        for rel_path in target_files:
            abs_path = self.root_dir / rel_path
            if not abs_path.is_file():
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for pattern in PATTERN_SIGNATURES:
                if pattern["id"] in matched_ids:
                    continue

                file_hint_match = bool(re.search(pattern["file_hint"], rel_path, re.IGNORECASE))
                regex_match = bool(re.search(pattern["regex"], content))

                if file_hint_match or regex_match:
                    item = dict(pattern)
                    item["code_anchor"] = f"{rel_path}"
                    detected.append(item)
                    matched_ids.add(pattern["id"])

        # 2. If target files yielded few matches, scan diff text
        if len(detected) < 2 and diff_text:
            for pattern in PATTERN_SIGNATURES:
                if pattern["id"] in matched_ids:
                    continue
                if re.search(pattern["regex"], diff_text):
                    item = dict(pattern)
                    item["code_anchor"] = "git diff inspection"
                    detected.append(item)
                    matched_ids.add(pattern["id"])

        # 3. Fallback to default high-yield DarkFac concepts if nothing matched
        if not detected:
            detected = [
                dict(PATTERN_SIGNATURES[0]),
                dict(PATTERN_SIGNATURES[1]),
                dict(PATTERN_SIGNATURES[3]),
            ]

        return detected
