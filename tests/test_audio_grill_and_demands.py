"""Reachability and deterministic unit tests for USR-60: Audio & Voice Intake / Grill.

Tests:
1. AudioTranscriptionEngine local & Groq cloud fallback.
2. FastAPI endpoint POST /api/audio/transcribe in Dark Hub.
3. TelegramGateway voice message ingestion (standalone /demand vs contextual Grill response).
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock
import httpx
import pytest
from fastapi.testclient import TestClient

from core.audio.engine import (
    AudioConfig,
    AudioTranscriptionEngine,
    TranscriptionResult,
)
from core.integrations.telegram import (
    TelegramActionType,
    TelegramConfig,
    TelegramGateway,
    TelegramUpdate,
)
from hub.backend.main import app


# ---------------------------------------------------------------------------
# Mock helpers for faster-whisper and Groq API
# ---------------------------------------------------------------------------

class MockSegment:
    def __init__(self, text: str, avg_logprob: float = -0.2) -> None:
        self.text = text
        self.avg_logprob = avg_logprob


class MockInfo:
    def __init__(self, duration: float = 3.5, language: str = "pt") -> None:
        self.duration = duration
        self.language = language


class MockWhisperSuccess:
    def transcribe(self, *args, **kwargs):
        segments = [
            MockSegment("Adicionar suporte a exportação CSV de relatórios de métricas", avg_logprob=-0.18),
        ]
        return segments, MockInfo(duration=3.5, language="pt")


class MockWhisperLowConfidence:
    def transcribe(self, *args, **kwargs):
        segments = [
            MockSegment("...ruído...", avg_logprob=-2.5),
        ]
        return segments, MockInfo(duration=1.0, language="pt")


# ---------------------------------------------------------------------------
# 1. Tests for core/audio/engine.py
# ---------------------------------------------------------------------------

def test_audio_engine_file_not_found():
    engine = AudioTranscriptionEngine()
    with pytest.raises(FileNotFoundError):
        engine.transcribe("non_existent_audio_file_12345.wav")


def test_audio_engine_local_transcription_success(tmp_path: Path):
    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"RIFFdummydata")

    cfg = AudioConfig(whisper_confidence_threshold=-1.0)
    engine = AudioTranscriptionEngine(config=cfg, local_model_instance=MockWhisperSuccess())

    result = engine.transcribe(audio_file)
    assert isinstance(result, TranscriptionResult)
    assert result.engine_used == "local_faster_whisper"
    assert not result.fallback_triggered
    assert "exportação CSV" in result.text
    assert result.confidence_score > 0.8
    assert result.language == "pt"


def test_audio_engine_fallback_to_groq(tmp_path: Path):
    audio_file = tmp_path / "noisy.wav"
    audio_file.write_bytes(b"RIFFdummydata")

    def mock_groq_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"text": "Texto transcrito com extrema clareza pelo Groq Whisper Cloud."},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_groq_handler))

    cfg = AudioConfig(
        groq_api_key="gsk_test_mock_token_123",
        whisper_confidence_threshold=-1.0,
    )
    engine = AudioTranscriptionEngine(
        config=cfg,
        local_model_instance=MockWhisperLowConfidence(),
        http_client=mock_client,
    )

    result = engine.transcribe(audio_file)
    assert result.fallback_triggered
    assert result.engine_used == "cloud_groq_whisper"
    assert "Groq Whisper Cloud" in result.text
    assert "confidence score too low" in (result.fallback_reason or "").lower()


# ---------------------------------------------------------------------------
# 2. Tests for FastAPI Dark Hub endpoint /api/audio/transcribe
# ---------------------------------------------------------------------------

def test_hub_audio_transcribe_endpoint(monkeypatch):
    client = TestClient(app)

    # Mock the internal AudioTranscriptionEngine inside HubService
    def mock_transcribe(self, save_path, language="pt"):
        return TranscriptionResult(
            text="Criar nova rota para integração com webhook",
            language=language or "pt",
            duration_seconds=2.8,
            confidence_score=0.95,
            avg_logprob=-0.12,
            engine_used="mock_engine",
            fallback_triggered=False,
            latency_ms=120.5,
        )

    monkeypatch.setattr(AudioTranscriptionEngine, "transcribe", mock_transcribe)

    wav_content = b"RIFFmockwavcontent12345678"
    res = client.post(
        "/api/audio/transcribe?language=pt",
        content=wav_content,
        headers={"Content-Type": "audio/wav"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["text"] == "Criar nova rota para integração com webhook"
    assert data["engine_used"] == "mock_engine"
    assert data["language"] == "pt"


# ---------------------------------------------------------------------------
# 3. Tests for TelegramGateway with Voice Input (USR-60)
# ---------------------------------------------------------------------------

def test_telegram_voice_standalone_creates_demand(tmp_path: Path):
    cfg = TelegramConfig(
        bot_token="test_token",
        authorized_user_ids=[12345],
        authorized_chat_ids=[99999],
    )

    demands_recorded = []

    def mock_demand_handler(text: str, user_id: int):
        demands_recorded.append((text, user_id))
        return {"ticket_id": "USR-AUDIO-1"}

    mock_engine = mock.MagicMock()
    mock_engine.transcribe.return_value = TranscriptionResult(
        text="Corrigir problema de concorrência nos jobs de banco",
        language="pt",
        engine_used="mock_whisper",
    )

    gw = TelegramGateway(
        config=cfg,
        state_dir=tmp_path,
        demand_handler=mock_demand_handler,
        audio_engine=mock_engine,
    )

    # Bypass download_telegram_file to succeed
    gw.download_telegram_file = mock.MagicMock(return_value=True)

    update_payload = {
        "update_id": 101,
        "message": {
            "message_id": 501,
            "date": 1720000000,
            "chat": {"id": 99999, "type": "private"},
            "from": {"id": 12345, "first_name": "Owner"},
            "voice": {
                "file_id": "voice_file_abc_123",
                "file_unique_id": "unique_123",
                "duration": 5,
                "mime_type": "audio/ogg",
            },
        },
    }

    result = gw.process_update(update_payload)
    assert result.authorized
    assert result.action == TelegramActionType.DEMAND
    assert result.target_id == "USR-AUDIO-1"
    assert "USR-AUDIO-1" in result.response_text
    assert len(demands_recorded) == 1
    assert "Corrigir problema de concorrência" in demands_recorded[0][0]


def test_telegram_voice_reply_to_grill_submits_answer(tmp_path: Path):
    cfg = TelegramConfig(
        bot_token="test_token",
        authorized_user_ids=[12345],
        authorized_chat_ids=[99999],
    )

    grills_submitted = []

    def mock_grill_handler(ticket_id: str, answer: str, user_id: int):
        grills_submitted.append((ticket_id, answer, user_id))
        return {"resumed": True}

    mock_engine = mock.MagicMock()
    mock_engine.transcribe.return_value = TranscriptionResult(
        text="Adotar a opção recomendada com validação em sandbox antes do deploy",
        language="pt",
        engine_used="mock_whisper",
    )

    gw = TelegramGateway(
        config=cfg,
        state_dir=tmp_path,
        grill_handler=mock_grill_handler,
        audio_engine=mock_engine,
    )
    gw.download_telegram_file = mock.MagicMock(return_value=True)

    update_payload = {
        "update_id": 102,
        "message": {
            "message_id": 502,
            "date": 1720000000,
            "chat": {"id": 99999, "type": "private"},
            "from": {"id": 12345, "first_name": "Owner"},
            "reply_to_message": {
                "message_id": 490,
                "date": 1719999000,
                "chat": {"id": 99999, "type": "private"},
                "text": "🔥 DarkHub: Intervenção Prioritária (Grill Pendente)\nDemanda: USR-60",
            },
            "voice": {
                "file_id": "voice_reply_xyz_789",
                "file_unique_id": "unique_789",
                "duration": 4,
                "mime_type": "audio/ogg",
            },
        },
    }

    result = gw.process_update(update_payload)
    assert result.authorized
    assert result.action == TelegramActionType.GRILL
    assert result.target_id == "USR-60"
    assert result.resumed is True
    assert "Resposta de voz registrada para o Grill de <b>USR-60</b>" in result.response_text
    assert len(grills_submitted) == 1
    assert grills_submitted[0][0] == "USR-60"
    assert "opção recomendada" in grills_submitted[0][1]
