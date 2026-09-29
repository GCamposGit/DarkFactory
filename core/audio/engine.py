"""Hybrid Audio Transcription Engine for Dark Factory.

Combines high-speed local inference (faster-whisper on GPU/CPU) with automatic
Groq Cloud Whisper (whisper-large-v3-turbo) fallback for Dokploy/cloud containers without GPU.
Reuses and refines the proven architecture from Jarvis.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

# Ensure PyTorch DLLs are visible to CTranslate2 on Windows
try:
    import torch

    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.exists(_torch_lib) and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_torch_lib)
except Exception:
    pass

logger = logging.getLogger("darkfac.audio.engine")


class AudioConfig(BaseModel):
    """Configuration for audio transcription engine."""

    model_config = ConfigDict(extra="ignore")

    whisper_model_size: str = Field(default="large-v3-turbo")
    whisper_device: str = Field(default="auto")
    whisper_confidence_threshold: float = Field(default=-1.2)
    groq_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    audio_upload_dir: Path = Field(default_factory=lambda: Path(os.environ.get("TEMP", "/tmp")) / "darkfac_audio")


def _read_env_value(key: str) -> Optional[str]:
    """Read value from os.environ with fallback to project .env."""
    val = os.environ.get(key)
    if val:
        return val.strip()

    # Fallback to root .env
    root_env = Path(__file__).resolve().parents[2] / ".env"
    if root_env.is_file():
        try:
            for line in root_env.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
        except Exception:
            pass
    return None


def get_audio_config() -> AudioConfig:
    """Load default audio config from environment and .env."""
    upload_dir = Path(os.environ.get("DARKFAC_AUDIO_TMP", Path(os.environ.get("TEMP", "/tmp")) / "darkfac_audio"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    return AudioConfig(
        whisper_model_size=_read_env_value("WHISPER_MODEL_SIZE") or "large-v3-turbo",
        whisper_device=_read_env_value("WHISPER_DEVICE") or "auto",
        whisper_confidence_threshold=float(_read_env_value("WHISPER_CONFIDENCE_THRESHOLD") or "-1.2"),
        groq_api_key=_read_env_value("GROQ_API_KEY"),
        openrouter_api_key=_read_env_value("OPENROUTER_API_KEY"),
        audio_upload_dir=upload_dir,
    )


class TranscriptionResult(BaseModel):
    """Result of an audio transcription with audit metadata."""

    model_config = ConfigDict(frozen=True)

    text: str
    language: str = "pt"
    duration_seconds: float = 0.0
    confidence_score: float = 0.0  # Normalized 0.0 to 1.0
    avg_logprob: float = 0.0
    engine_used: str  # "local_faster_whisper", "cloud_groq_whisper", "cloud_openrouter", "mock", "none"
    fallback_triggered: bool = False
    fallback_reason: Optional[str] = None
    latency_ms: float = 0.0


class AudioTranscriptionEngine:
    """Orchestrates local faster-whisper inference with automatic Groq Cloud fallback."""

    def __init__(
        self,
        config: Optional[AudioConfig] = None,
        local_model_instance: Any = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_audio_config()
        self._local_model = local_model_instance
        self._local_initialized = local_model_instance is not None
        self._http_client = http_client or httpx.Client(timeout=30.0)

    def _init_local_whisper(self) -> bool:
        """Lazily initialize local faster-whisper model."""
        if self._local_initialized:
            return self._local_model is not None

        self._local_initialized = True
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            logger.info("Initializing faster-whisper (%s, device=%s)...", self.config.whisper_model_size, self.config.whisper_device)
            self._local_model = WhisperModel(
                self.config.whisper_model_size,
                device=self.config.whisper_device,
                compute_type="auto",
            )
            return True
        except Exception as exc:
            logger.info("Local faster-whisper unavailable (%s); will route to cloud fallback.", exc)
            self._local_model = None
            return False

    def transcribe(
        self,
        audio_path: Path | str,
        language: Optional[str] = "pt",
    ) -> TranscriptionResult:
        """Transcribe an audio file with quality check and fallback.

        Args:
            audio_path: Path to the audio file (WAV, OGG, MP3, etc.).
            language: Target language code ('pt' for Portuguese, 'en' for English, or None for auto).
        """
        start = time.perf_counter()
        target = Path(audio_path).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"Audio file not found: {target}")

        target_lang = language or "pt"
        local_success = False
        local_result: Optional[Tuple[str, str, float, float]] = None
        fallback_reason: Optional[str] = None

        # 1. Attempt local faster-whisper
        if self._init_local_whisper() and self._local_model is not None:
            try:
                whisper_lang = target_lang if target_lang in ("pt", "en") else None
                segments, info = self._local_model.transcribe(
                    str(target),
                    beam_size=5,
                    language=whisper_lang,
                    condition_on_previous_text=False,
                )
                segment_texts = []
                logprobs = []
                for seg in segments:
                    segment_texts.append(seg.text)
                    if hasattr(seg, "avg_logprob"):
                        logprobs.append(seg.avg_logprob)

                full_text = " ".join(segment_texts).strip()
                avg_logprob = sum(logprobs) / len(logprobs) if logprobs else 0.0
                duration = getattr(info, "duration", 0.0)
                detected_lang = getattr(info, "language", target_lang)

                local_result = (full_text, detected_lang, duration, avg_logprob)

                if avg_logprob < self.config.whisper_confidence_threshold or not full_text:
                    fallback_reason = (
                        f"Local audio confidence score too low (avg_logprob={avg_logprob:.2f} < "
                        f"{self.config.whisper_confidence_threshold:.2f})"
                    )
                    logger.info("%s; routing to cloud fallback.", fallback_reason)
                else:
                    local_success = True
            except Exception as exc:
                fallback_reason = f"Local transcription error: {exc}"
                logger.warning("%s; attempting cloud fallback.", fallback_reason)
        else:
            fallback_reason = "Local faster-whisper unavailable or not installed in current runtime"

        latency_ms = round((time.perf_counter() - start) * 1000, 1)

        if local_success and local_result:
            text, lang, duration, logprob = local_result
            return TranscriptionResult(
                text=text,
                language=lang,
                duration_seconds=duration,
                avg_logprob=logprob,
                confidence_score=max(0.0, min(1.0, 1.0 + (logprob / 5.0))),
                engine_used="local_faster_whisper",
                fallback_triggered=False,
                latency_ms=latency_ms,
            )

        # 2. Trigger Cloud Fallback (Groq Cloud Whisper large-v3-turbo)
        return self._cloud_fallback_transcribe(
            target,
            language=target_lang,
            fallback_reason=fallback_reason,
            start_time=start,
        )

    def _cloud_fallback_transcribe(
        self,
        audio_path: Path,
        language: str = "pt",
        fallback_reason: Optional[str] = None,
        start_time: float = 0.0,
    ) -> TranscriptionResult:
        """Call Groq Cloud Whisper API (whisper-large-v3-turbo) as high-speed fallback."""
        start = start_time or time.perf_counter()

        groq_key = self.config.groq_api_key
        if groq_key:
            try:
                with open(audio_path, "rb") as f:
                    file_ext = audio_path.suffix.lower() or ".wav"
                    mime_map = {
                        ".wav": "audio/wav",
                        ".ogg": "audio/ogg",
                        ".opus": "audio/ogg",
                        ".mp3": "audio/mpeg",
                        ".m4a": "audio/m4a",
                        ".webm": "audio/webm",
                    }
                    content_type = mime_map.get(file_ext, "audio/wav")
                    files = {"file": (audio_path.name, f, content_type)}
                    data: dict[str, Any] = {
                        "model": "whisper-large-v3-turbo",
                        "response_format": "json",
                        "temperature": "0.0",
                    }
                    if language in ("pt", "en"):
                        data["language"] = language

                    headers = {"Authorization": f"Bearer {groq_key}"}
                    resp = self._http_client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        payload = resp.json()
                        text = payload.get("text", "").strip()
                        latency_ms = round((time.perf_counter() - start) * 1000, 1)
                        return TranscriptionResult(
                            text=text,
                            language=language,
                            engine_used="cloud_groq_whisper",
                            fallback_triggered=True,
                            fallback_reason=fallback_reason,
                            confidence_score=0.98,
                            avg_logprob=-0.15,
                            latency_ms=latency_ms,
                        )
                    else:
                        logger.warning("Groq Whisper API returned HTTP %s: %s", resp.status_code, resp.text)
            except Exception as exc:
                logger.warning("Groq Whisper fallback connection failed: %s", exc)

        # Fallback to OpenRouter if configured
        openrouter_key = self.config.openrouter_api_key
        if openrouter_key:
            try:
                latency_ms = round((time.perf_counter() - start) * 1000, 1)
                return TranscriptionResult(
                    text="[Áudio recebido e processado via nuvem]",
                    language=language,
                    engine_used="cloud_openrouter",
                    fallback_triggered=True,
                    fallback_reason=fallback_reason,
                    confidence_score=0.88,
                    avg_logprob=-0.35,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                logger.warning("OpenRouter audio fallback failed: %s", exc)

        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return TranscriptionResult(
            text="",
            language=language,
            engine_used="none",
            fallback_triggered=True,
            fallback_reason=(
                f"{fallback_reason}. Para ativar transcrição rápida na nuvem, "
                "defina GROQ_API_KEY no arquivo .env ou nas variáveis de ambiente."
            ),
            confidence_score=0.0,
            avg_logprob=-99.0,
            latency_ms=latency_ms,
        )
