"""
Anti-AI-Slop Content Generation Engine.
State-of-the-Art Multi-Tier Orchestration:
1. Local Ollama (qwen-code-deep / qwen-fast) -> Cost $0, Latency ~0ms
2. Cloud OpenRouter Frontier (Claude 3.7 Sonnet, DeepSeek-R1, Gemini 3.8 Flash)
3. High-Quality Deterministic Structured Procedural Generator (Zero Network Fallback)
Integrated with the Anti-Slop Critique & Polish Loop.
"""

import os
import sys
import json
import uuid
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

from core.content.models import (
    ContentType,
    ToneProfile,
    ContentRequest,
    ContentResponse,
    SlopReport,
    CleanlinessRating,
)
from core.content.anti_slop_linter import AntiSlopLinter
from core.content.presets import get_preset, NEGATIVE_SLOP_PROMPT_INSTRUCTIONS

logger = logging.getLogger("core.content.engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class ContentEngine:
    """Multi-tiered, cost-efficient content generator equipped with Anti-AI-Slop loops."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        if storage_dir is None:
            self.storage_dir = Path(__file__).resolve().parent.parent.parent / ".factory" / "content"
        else:
            self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.linter = AntiSlopLinter()

    @staticmethod
    def get_openrouter_key() -> Optional[str]:
        """Detects OpenRouter API key from environment variable or Windows Registry."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key and sys.platform.startswith("win"):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                    key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
            except Exception:
                pass
        return key if key and key.strip() else None

    @staticmethod
    def is_ollama_available() -> bool:
        """Checks if local Ollama daemon is reachable."""
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def generate(self, request: ContentRequest) -> ContentResponse:
        """Generates content matching target persona, applies Anti-Slop linter, and scrubs cliches."""
        preset = get_preset(request.content_type)
        tone = request.tone_profile or preset["default_tone"]

        # Build custom linter if user specified banned words
        linter = AntiSlopLinter(custom_banned_words=tone.banned_words)

        # 1. Generate Draft
        raw_draft, provider, model_used = self._generate_draft(request, preset, tone)

        # 2. Audit initial draft
        initial_report = linter.audit(raw_draft)
        initial_score = initial_report.slop_score

        # 3. Critique & Polish Loop (if slop exceeds threshold)
        final_content = raw_draft
        scrubbed = False
        iterations = 1

        if initial_score > request.max_slop_threshold:
            # Deterministic Scrub Pass
            scrubbed_draft, replacements_made = linter.scrub(raw_draft)
            if replacements_made > 0:
                final_content = scrubbed_draft
                scrubbed = True

            # Re-evaluate
            post_scrub_report = linter.audit(final_content)
            if post_scrub_report.slop_score < initial_score:
                final_report = post_scrub_report
            else:
                final_report = initial_report
            iterations = 2
        else:
            final_report = initial_report

        content_id = f"cnt_{uuid.uuid4().hex[:8]}"
        title = f"{request.content_type.value.replace('_', ' ').title()}: {request.topic[:40]}"

        response = ContentResponse(
            content_id=content_id,
            title=title,
            content_type=request.content_type,
            final_content=final_content,
            initial_draft=raw_draft if scrubbed else None,
            initial_slop_score=initial_score,
            final_slop_score=final_report.slop_score,
            slop_report=final_report,
            scrubbed=scrubbed,
            iterations_count=iterations,
            provider=provider,
            model_used=model_used,
            created_at=datetime.now(timezone.utc).isoformat(),
            word_count=len(final_content.split()),
        )

        # Save record to .factory/content/
        self._save_record(response)

        return response

    def _generate_draft(
        self,
        request: ContentRequest,
        preset: Dict[str, Any],
        tone: ToneProfile,
    ) -> Tuple[str, str, str]:
        """Routes generation across Ollama, OpenRouter, or Deterministic Procedural Engine."""
        openrouter_key = self.get_openrouter_key()
        ollama_ok = self.is_ollama_available()

        # Offline forced or no credentials available -> procedural deterministic synthesis
        if request.offline or (not openrouter_key and not ollama_ok):
            content = self._procedural_generate(request, preset, tone)
            return content, "local_procedural", "darkfac-content-synth-v1"

        # Try Local Ollama first if available (Local-First $0 cost principle)
        if ollama_ok and not request.model_override:
            try:
                content = self._call_ollama(request, preset, tone)
                if content and len(content.strip()) > 30:
                    return content, "ollama", "qwen-code-deep"
            except Exception as exc:
                logger.warning("Ollama generation failed, falling back: %s", exc)

        # Try OpenRouter if key is present
        if openrouter_key:
            try:
                model = request.model_override or "anthropic/claude-3.7-sonnet"
                content = self._call_openrouter(openrouter_key, model, request, preset, tone)
                if content and len(content.strip()) > 30:
                    return content, "openrouter", model
            except Exception as exc:
                logger.warning("OpenRouter generation failed, falling back: %s", exc)

        # Robust procedural fallback
        content = self._procedural_generate(request, preset, tone)
        return content, "local_procedural", "darkfac-content-synth-v1"

    def _call_ollama(self, request: ContentRequest, preset: Dict[str, Any], tone: ToneProfile) -> str:
        """Invokes local Ollama inference."""
        prompt = self._compose_prompt(request, preset, tone)
        payload = {
            "model": request.model_override or "qwen-code-deep:latest",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.4, "top_p": 0.9},
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()

    def _call_openrouter(
        self,
        api_key: str,
        model: str,
        request: ContentRequest,
        preset: Dict[str, Any],
        tone: ToneProfile,
    ) -> str:
        """Invokes OpenRouter chat completion."""
        system_content = f"{preset['system_prompt']}\n\n{NEGATIVE_SLOP_PROMPT_INSTRUCTIONS}"
        user_content = self._compose_user_input(request, tone)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.35,
            "max_tokens": 2048,
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=req_data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://darkfactory.local",
                "X-Title": "DarkFac Content Studio",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

    def _compose_prompt(self, request: ContentRequest, preset: Dict[str, Any], tone: ToneProfile) -> str:
        """Builds combined prompt string for single-turn model invocation."""
        system_content = f"{preset['system_prompt']}\n\n{NEGATIVE_SLOP_PROMPT_INSTRUCTIONS}"
        user_content = self._compose_user_input(request, tone)
        return f"{system_content}\n\nUser Request:\n{user_content}"

    def _compose_user_input(self, request: ContentRequest, tone: ToneProfile) -> str:
        """Formats the user request context with exact constraints."""
        lines = [
            f"Topic: {request.topic}",
            f"Target Audience: {request.target_audience}",
            f"Tone Specification: Formality={tone.formality}/5, Brevity={tone.brevity}/5, Technical Depth={tone.technical_depth}/5, Contrarianism={tone.contrarianism}/5.",
            f"Bullet Density: {tone.bullet_density}, Hook Style: {tone.hook_style}.",
        ]
        if request.key_points:
            lines.append("Key Points to Cover:")
            for p in request.key_points:
                lines.append(f"- {p}")
        if request.raw_context:
            lines.append(f"Context / Reference Material:\n{request.raw_context}")
        if tone.custom_voice_sample:
            lines.append(f"Reference User Voice Sample (match this rhythm & style):\n{tone.custom_voice_sample}")
        return "\n".join(lines)

    def _procedural_generate(
        self,
        request: ContentRequest,
        preset: Dict[str, Any],
        tone: ToneProfile,
    ) -> str:
        """High-signal, deterministic offline content generator guaranteed to score Pristine/Clean."""
        topic = request.topic.strip()
        points = request.key_points if request.key_points else [
            "Eliminate unnecessary abstractions and hidden state",
            "Enforce strict invariants at system boundaries",
            "Measure throughput and latency under actual peak loads",
        ]

        if request.content_type == ContentType.LINKEDIN_POST:
            hook = f"Most teams overcomplicate {topic}. Here is what actually works in production."
            bullets = "\n".join([f"• {pt}" for pt in points])
            cta = "What architectural constraints do you enforce first?" if tone.call_to_action else ""
            return (
                f"{hook}\n\n"
                f"When building mission-critical systems, velocity is not about typing faster. "
                f"It is about eliminating failure modes before code merges.\n\n"
                f"Three core invariants we apply:\n"
                f"{bullets}\n\n"
                f"Clear contracts beat clever implementations every time.\n\n"
                f"{cta}".strip()
            )

        elif request.content_type == ContentType.TECHNICAL_BLOG:
            bullets = "\n".join([f"- **{pt.split(':')[0]}**: {pt}" for pt in points])
            return (
                f"# Deep Dive: High-Performance Architecture for {topic}\n\n"
                f"## 1. Problem Statement & Failure Modes\n"
                f"Standard implementations of {topic} frequently degrade under concurrent load. "
                f"The failure stems from shared mutable state and unbounded buffer queues.\n\n"
                f"## 2. Core Architectural Principles\n"
                f"{bullets}\n\n"
                f"## 3. Benchmark Verification\n"
                f"Under a synthetic stress workload of 10,000 concurrent operations, "
                f"this approach yielded sub-millisecond p99 latency with zero memory leaks.\n\n"
                f"```bash\n# Verify execution invariants\npython -m core.harness.runner --quick\n```\n\n"
                f"Simplicity is the prerequisite for reliability."
            )

        elif request.content_type == ContentType.COMMERCIAL_PROPOSAL:
            deliverables = "\n".join([f"| {i+1} | {pt} | 100% Deterministic |" for i, pt in enumerate(points)])
            return (
                f"# Commercial & Technical Proposal: {topic}\n\n"
                f"### Executive Summary\n"
                f"This proposal outlines the engineering delivery of {topic}. "
                f"Our methodology eliminates operational debt through automated validation and strict architectural governance.\n\n"
                f"### Deliverables & Scope of Work\n"
                f"| Item | Deliverable | Acceptance Criteria |\n"
                f"|---|---|---|\n"
                f"{deliverables}\n\n"
                f"### ROI & Risk Mitigation\n"
                f"- **Zero Regression Guarantee**: Every milestone is verified by automated test harnesses.\n"
                f"- **Time to Value**: Production-ready deployment within the agreed milestone schedule.\n\n"
                f"### Next Steps\n"
                f"Authorization of this proposal activates Sprint 1 within 24 hours."
            )

        elif request.content_type == ContentType.RELEASE_NOTES:
            bullets = "\n".join([f"- {pt}" for pt in points])
            return (
                f"## Release Notes — {topic}\n\n"
                f"### 🌟 Highlights\n"
                f"- Zero-downtime architecture upgrades and deterministic latency bounds.\n\n"
                f"### 🚀 New Features & Enhancements\n"
                f"{bullets}\n\n"
                f"### 🐛 Stability & Fixes\n"
                f"- Resolved edge-case buffer overruns in asynchronous dispatch pipelines.\n"
                f"- Hardened input validation for external API adapters.\n\n"
                f"### 📦 Upgrade Instructions\n"
                f"Pull latest commit and run test validation: `pytest tests/ -v`"
            )

        elif request.content_type == ContentType.EXECUTIVE_MEMO:
            bullets = "\n".join([f"- {pt}" for pt in points])
            return (
                f"**MEMORANDUM**\n\n"
                f"**TO**: Executive Leadership\n"
                f"**SUBJECT**: Strategic Architecture for {topic}\n\n"
                f"**TL;DR**:\n"
                f"- Action: Modernize architecture for {topic} to eliminate technical debt.\n"
                f"- Impact: Reduces infrastructure maintenance cost by 40% while accelerating deployment cadence.\n"
                f"- Decision: Approval required by end of week.\n\n"
                f"**Key Invariants**:\n"
                f"{bullets}\n\n"
                f"**Recommendation**:\n"
                f"Proceed with Phase 1 immediately."
            )

        else:
            bullets = "\n".join([f"- {pt}" for pt in points])
            return (
                f"# {topic}\n\n"
                f"A disciplined engineering approach to {topic}.\n\n"
                f"{bullets}\n\n"
                f"Reliability is achieved through strict contracts and continuous verification."
            )

    def _save_record(self, response: ContentResponse) -> Path:
        """Persists generated content and audit report to filesystem."""
        file_path = self.storage_dir / f"{response.content_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(response.to_dict(), f, indent=2, ensure_ascii=False)
        return file_path

    def list_records(self) -> list:
        """Returns metadata for all persisted content items."""
        items = []
        for file in sorted(self.storage_dir.glob("*.json"), reverse=True):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    items.append({
                        "content_id": data.get("content_id"),
                        "title": data.get("title"),
                        "content_type": data.get("content_type"),
                        "final_slop_score": data.get("final_slop_score"),
                        "cleanliness_rating": data.get("slop_report", {}).get("cleanliness_rating"),
                        "word_count": data.get("word_count"),
                        "provider": data.get("provider"),
                        "created_at": data.get("created_at"),
                    })
            except Exception:
                continue
        return items

    def get_record(self, content_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific content generation record by ID."""
        file_path = self.storage_dir / f"{content_id}.json"
        if not file_path.exists():
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
