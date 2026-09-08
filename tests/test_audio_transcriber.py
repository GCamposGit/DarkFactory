"""Offline and opt-in live tests for ``core.audio.transcriber``."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.audio.transcriber import (
    AudioTranscriber,
    MeetingTranscript,
    TranscriptSegment,
)


@pytest.mark.offline
def test_transcript_models_and_formatting() -> None:
    seg1 = TranscriptSegment(
        start=0.5, end=2.0, speaker="Local", text="Olá time", channel_index=0
    )
    seg2 = TranscriptSegment(
        start=1.8,
        end=3.5,
        speaker="Remoto",
        text="Hello everyone",
        channel_index=1,
    )

    transcript = MeetingTranscript(
        audio_path="test_audio.wav",
        duration_sec=4.0,
        processing_time_sec=0.2,
        realtime_factor=20.0,
        is_dual_channel=True,
        detected_language="pt",
        segments=[seg1, seg2],
    )

    assert transcript.full_text == "Olá time Hello everyone"
    md = transcript.to_markdown()
    assert "# Transcrição da Reunião" in md
    assert "**[Local]**" in md
    assert "**[Remoto]**" in md
    assert "20.0x tempo real" in md


@pytest.mark.offline
def test_channel_normalization() -> None:
    low_audio = np.array([0.05, -0.05, 0.1, -0.1], dtype=np.float32)
    normalized = AudioTranscriber._normalize_channel(low_audio, target_peak=0.95)
    assert np.isclose(np.max(np.abs(normalized)), 0.95)


@dataclass(frozen=True)
class _FakeSegment:
    start: float
    end: float
    text: str
    avg_logprob: float


@dataclass(frozen=True)
class _FakeInfo:
    language: str


class _FakeWhisperModel:
    """Small deterministic substitute for faster-whisper's model contract."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def transcribe(
        self, audio: Any, **kwargs: Any
    ) -> tuple[list[_FakeSegment], _FakeInfo]:
        self.calls.append({"audio": audio, **kwargs})
        channel_index = len(self.calls) - 1
        return (
            [
                _FakeSegment(
                    start=0.4 + channel_index,
                    end=0.9 + channel_index,
                    text=f" canal {channel_index} ",
                    avg_logprob=-0.12 - channel_index,
                )
            ],
            _FakeInfo(language="pt"),
        )


@pytest.mark.offline
def test_transcribe_stereo_fixture_offline(
    stereo_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model = _FakeWhisperModel()
    monkeypatch.setattr(AudioTranscriber, "_ensure_model", lambda self: fake_model)

    transcriber = AudioTranscriber(
        model_size="unit-test", device="cpu", compute_type="float32"
    )
    result = transcriber.transcribe_file(
        audio_path=stereo_wav,
        speaker_0_label="Maria (Local)",
        speaker_1_label="David (Remoto)",
        language="pt",
        beam_size=1,
        vad_filter=False,
    )

    assert isinstance(result, MeetingTranscript)
    assert result.is_dual_channel is True
    assert result.detected_language == "pt"
    assert [segment.speaker for segment in result.segments] == [
        "Maria (Local)",
        "David (Remoto)",
    ]
    assert result.full_text == "canal 0 canal 1"
    assert len(fake_model.calls) == 2
    assert all(isinstance(call["audio"], np.ndarray) for call in fake_model.calls)
    assert all(call["beam_size"] == 1 for call in fake_model.calls)
    assert all(call["vad_filter"] is False for call in fake_model.calls)


@pytest.mark.offline
def test_transcribe_mono_fixture_offline(
    stereo_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model = _FakeWhisperModel()
    monkeypatch.setattr(AudioTranscriber, "_ensure_model", lambda self: fake_model)

    result = AudioTranscriber(
        model_size="unit-test", device="cpu", compute_type="float32"
    ).transcribe_file(
        audio_path=stereo_wav,
        is_dual_channel=False,
        language="pt",
    )

    assert result.is_dual_channel is False
    assert len(result.segments) == 1
    assert result.segments[0].speaker == "Participante"
    assert result.segments[0].channel_index == 0
    assert fake_model.calls[0]["audio"] == str(stereo_wav)


@pytest.mark.live
@pytest.mark.gpu
def test_transcribe_stereo_meeting_file_live_gpu() -> None:
    """Manual experiment; never part of the official offline suite."""

    pytest.importorskip("torch")
    pytest.importorskip("faster_whisper")
    import torch

    if not torch.cuda.is_available():
        pytest.skip("CUDA não disponível para o ensaio live de áudio.")

    audio_path = Path("audio_bench") / "meeting_stereo.wav"
    if not audio_path.exists():
        pytest.skip("Arquivo audio_bench/meeting_stereo.wav não encontrado.")

    transcriber = AudioTranscriber(
        model_size="large-v3-turbo", device="cuda", compute_type="float16"
    )
    result = transcriber.transcribe_file(
        audio_path=audio_path,
        speaker_0_label="Maria (Local)",
        speaker_1_label="David (Remoto)",
    )

    assert isinstance(result, MeetingTranscript)
    assert result.is_dual_channel is True
    assert result.duration_sec >= 15.0
    assert len(result.segments) >= 2

    speakers = {segment.speaker for segment in result.segments}
    assert "Maria (Local)" in speakers
    assert "David (Remoto)" in speakers
