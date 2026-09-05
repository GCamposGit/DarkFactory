"""
Testes automatizados para o serviço local de transcrição (core/audio/transcriber.py).
"""

import os
from pathlib import Path
import pytest
import numpy as np

from core.audio.transcriber import (
    AudioTranscriber,
    MeetingTranscript,
    TranscriptSegment,
)


def test_transcript_models_and_formatting():
    seg1 = TranscriptSegment(start=0.5, end=2.0, speaker="Local", text="Olá time", channel_index=0)
    seg2 = TranscriptSegment(start=1.8, end=3.5, speaker="Remoto", text="Hello everyone", channel_index=1)

    transcript = MeetingTranscript(
        audio_path="test_audio.wav",
        duration_sec=4.0,
        processing_time_sec=0.2,
        realtime_factor=20.0,
        is_dual_channel=True,
        detected_language="pt",
        segments=[seg1, seg2]
    )

    assert transcript.full_text == "Olá time Hello everyone"
    md = transcript.to_markdown()
    assert "# Transcrição da Reunião" in md
    assert "**[Local]**" in md
    assert "**[Remoto]**" in md
    assert "20.0x tempo real" in md


def test_channel_normalization():
    # Áudio de baixo volume
    low_audio = np.array([0.05, -0.05, 0.1, -0.1], dtype=np.float32)
    normalized = AudioTranscriber._normalize_channel(low_audio, target_peak=0.95)
    assert np.isclose(np.max(np.abs(normalized)), 0.95)


def test_transcribe_stereo_meeting_file():
    audio_path = Path("audio_bench") / "meeting_stereo.wav"
    if not audio_path.exists():
        pytest.skip("Arquivo audio_bench/meeting_stereo.wav não encontrado.")

    transcriber = AudioTranscriber(model_size="large-v3-turbo")
    result = transcriber.transcribe_file(
        audio_path=audio_path,
        speaker_0_label="Maria (Local)",
        speaker_1_label="David (Remoto)"
    )

    assert isinstance(result, MeetingTranscript)
    assert result.is_dual_channel is True
    assert result.duration_sec >= 15.0
    assert len(result.segments) >= 2
    
    speakers = {s.speaker for s in result.segments}
    assert "Maria (Local)" in speakers
    assert "David (Remoto)" in speakers
