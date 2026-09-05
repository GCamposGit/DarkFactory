"""
High-Performance Local Audio Transcription Service (faster-whisper).
Designed for headless programmatic invocation by LLMs and autonomous agents.
Optimized for NVIDIA RTX 4070 (CUDA FP16) with native dual-channel meeting support.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple, Literal, Union

# No Windows, inicializa o runtime do PyTorch para disponibilizar cublas64_12.dll e cudnn para o CTranslate2
try:
    import torch
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.exists(_torch_lib) and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_torch_lib)
except Exception:
    pass

import numpy as np
import soundfile as sf
from pydantic import BaseModel, Field

logger = logging.getLogger("darkfac.audio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class TranscriptSegment(BaseModel):
    start: float = Field(..., description="Tempo inicial em segundos")
    end: float = Field(..., description="Tempo final em segundos")
    speaker: str = Field(..., description="Identificador do falante (ex: Local, Remoto, Speaker 0)")
    text: str = Field(..., description="Texto transcrito do segmento")
    channel_index: Optional[int] = Field(default=None, description="Índice do canal de áudio (0 para esquerdo, 1 para direito)")
    confidence: Optional[float] = Field(default=None, description="Probabilidade média de confiança da transcrição")


class MeetingTranscript(BaseModel):
    audio_path: str = Field(..., description="Caminho do arquivo de áudio processado")
    duration_sec: float = Field(..., description="Duração total do áudio em segundos")
    processing_time_sec: float = Field(..., description="Tempo de processamento na GPU em segundos")
    realtime_factor: float = Field(..., description="Fator de velocidade em relação ao tempo real (ex: 20x)")
    is_dual_channel: bool = Field(..., description="Se o áudio foi processado como dual-channel estéreo")
    detected_language: Optional[str] = Field(default=None, description="Código do idioma detectado (ex: pt, en)")
    segments: List[TranscriptSegment] = Field(default_factory=list, description="Lista cronológica de falas")

    @property
    def full_text(self) -> str:
        """Retorna a transcrição contínua sem metadados."""
        return " ".join(s.text.strip() for s in self.segments)

    def to_markdown(self, include_timestamps: bool = True) -> str:
        """Formata a transcrição em markdown estruturado, ideal para ingestão por LLMs."""
        lines = [
            f"# Transcrição da Reunião: {Path(self.audio_path).name}",
            f"- **Duração**: {self.duration_sec:.1f}s | **Processamento**: {self.processing_time_sec:.2f}s ({self.realtime_factor:.1f}x tempo real)",
            f"- **Modo**: {'Dual-Channel (Estéreo Separado)' if self.is_dual_channel else 'Mono Misto'}",
            "",
            "## Registro de Falas",
            ""
        ]

        for s in self.segments:
            speaker_tag = f"**[{s.speaker}]**"
            time_tag = f" `[{s.start:.1f}s - {s.end:.1f}s]`" if include_timestamps else ""
            lines.append(f"- {time_tag} {speaker_tag}: {s.text.strip()}")

        return "\n".join(lines)


class AudioTranscriber:
    """
    Serviço singleton para gerenciar o modelo faster-whisper na GPU
    e processar áudios de reunião sem overhead de recarga de modelo.
    """
    _instance: Optional[AudioTranscriber] = None
    _model = None
    _loaded_model_size: Optional[str] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(AudioTranscriber, cls).__new__(cls)
        return cls._instance

    def __init__(
        self,
        model_size: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        cpu_threads: int = 8
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads

    def _ensure_model(self):
        """Carrega o modelo lazy na GPU e preserva na memória para chamadas subsequentes."""
        if AudioTranscriber._model is None or AudioTranscriber._loaded_model_size != self.model_size:
            from faster_whisper import WhisperModel
            logger.info(f"Carregando faster-whisper ({self.model_size}) no dispositivo {self.device} ({self.compute_type})...")
            start = time.perf_counter()
            AudioTranscriber._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads
            )
            AudioTranscriber._loaded_model_size = self.model_size
            elapsed = time.perf_counter() - start
            logger.info(f"Modelo faster-whisper carregado na GPU em {elapsed:.2f}s")
        return AudioTranscriber._model

    @staticmethod
    def _normalize_channel(audio: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
        """Aplica normalização de pico para equilibrar canais com volume baixo sem distorção."""
        peak = np.max(np.abs(audio))
        if peak > 1e-6:
            return (audio / peak * target_peak).astype(np.float32)
        return audio.astype(np.float32)

    def transcribe_file(
        self,
        audio_path: Union[str, Path],
        is_dual_channel: Optional[bool] = None,
        language: Optional[str] = None,
        speaker_0_label: str = "Local (Canal 0)",
        speaker_1_label: str = "Remoto (Canal 1)",
        beam_size: int = 5,
        vad_filter: bool = True
    ) -> MeetingTranscript:
        """
        Transcreve um arquivo de áudio local com otimização automática para reuniões.
        
        Args:
            audio_path: Caminho do arquivo de áudio (wav, mp3, m4a, flac, etc.)
            is_dual_channel: Se True, força separação estéreo. Se None, detecta automaticamente pelos canais do arquivo.
            language: Idioma forçado (ex: 'pt', 'en', 'es') ou None para detecção automática.
            speaker_0_label: Nome do falante no canal 0 (esquerdo).
            speaker_1_label: Nome do falante no canal 1 (direito).
            beam_size: Tamanho do beam search (5 para qualidade máxima).
            vad_filter: Ativa filtro de silêncio e corte de ruídos de fundo.
        """
        path_str = str(audio_path)
        if not os.path.exists(path_str):
            raise FileNotFoundError(f"Arquivo de áudio não encontrado: {path_str}")

        model = self._ensure_model()

        # Inspecionar propriedades do arquivo
        info = sf.info(path_str)
        duration_sec = info.duration
        num_channels = info.channels

        # Decidir se aplica dual-channel
        use_dual = is_dual_channel if is_dual_channel is not None else (num_channels >= 2)

        start_time = time.perf_counter()
        segments_result: List[TranscriptSegment] = []
        detected_lang: Optional[str] = language

        if use_dual and num_channels >= 2:
            logger.info(f"Processando áudio dual-channel ({num_channels} canais): {Path(path_str).name}")
            data, _ = sf.read(path_str, dtype="float32")
            
            # Normalizar canais individualmente para resolver disparidades de volume
            ch0 = self._normalize_channel(data[:, 0])
            ch1 = self._normalize_channel(data[:, 1])

            # Transcrever Canal 0
            seg_ch0, info_0 = model.transcribe(ch0, language=language, beam_size=beam_size, vad_filter=vad_filter)
            for s in seg_ch0:
                segments_result.append(TranscriptSegment(
                    start=round(s.start, 2),
                    end=round(s.end, 2),
                    speaker=speaker_0_label,
                    text=s.text.strip(),
                    channel_index=0,
                    confidence=round(getattr(s, "avg_logprob", 0.0), 3)
                ))

            # Transcrever Canal 1
            seg_ch1, info_1 = model.transcribe(ch1, language=language, beam_size=beam_size, vad_filter=vad_filter)
            for s in seg_ch1:
                segments_result.append(TranscriptSegment(
                    start=round(s.start, 2),
                    end=round(s.end, 2),
                    speaker=speaker_1_label,
                    text=s.text.strip(),
                    channel_index=1,
                    confidence=round(getattr(s, "avg_logprob", 0.0), 3)
                ))

            # Intercalar cronologicamente por tempo de início
            segments_result.sort(key=lambda s: s.start)
            detected_lang = info_0.language if hasattr(info_0, "language") else language

        else:
            logger.info(f"Processando áudio mono ({num_channels} canal): {Path(path_str).name}")
            seg_mono, info_mono = model.transcribe(path_str, language=language, beam_size=beam_size, vad_filter=vad_filter)
            for s in seg_mono:
                segments_result.append(TranscriptSegment(
                    start=round(s.start, 2),
                    end=round(s.end, 2),
                    speaker="Participante",
                    text=s.text.strip(),
                    channel_index=0,
                    confidence=round(getattr(s, "avg_logprob", 0.0), 3)
                ))
            detected_lang = info_mono.language if hasattr(info_mono, "language") else language

        proc_time = time.perf_counter() - start_time
        rtf = duration_sec / proc_time if proc_time > 0 else 0.0

        logger.info(f"Transcrição finalizada em {proc_time:.2f}s ({rtf:.1f}x tempo real). Total de segmentos: {len(segments_result)}")

        return MeetingTranscript(
            audio_path=path_str,
            duration_sec=duration_sec,
            processing_time_sec=round(proc_time, 2),
            realtime_factor=round(rtf, 1),
            is_dual_channel=use_dual,
            detected_language=detected_lang,
            segments=segments_result
        )


# Instância utilitária global para reuso imediato
transcriber = AudioTranscriber()
