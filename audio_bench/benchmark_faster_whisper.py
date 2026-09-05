#!/usr/bin/env python3
"""
Benchmark para Whisper Large-v3-Turbo via faster-whisper na RTX 4070.
Testa:
1. Modo Mono Misto (com sobreposição e ruído)
2. Modo Dual-Channel Estéreo (processamento por canal + fusão temporal)
"""

import os
import sys
import time
import torch
import soundfile as sf
import numpy as np
from faster_whisper import WhisperModel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AUDIO_DIR = os.path.dirname(__file__)
STEREO_PATH = os.path.join(AUDIO_DIR, "meeting_stereo.wav")
MONO_PATH = os.path.join(AUDIO_DIR, "meeting_mono_mixed.wav")

def test_whisper():
    print("=" * 65)
    print(" Benchmark: faster-whisper (Large-v3-Turbo em CUDA FP16)")
    print("=" * 65)

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    load_start = time.perf_counter()
    # Carregar modelo large-v3-turbo em GPU com FP16
    model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    load_time = time.perf_counter() - load_start
    print(f"-> Modelo carregado na GPU em: {load_time:.2f}s")

    # ==========================================================
    # TESTE 1: Áudio Mono Misto (fala colidindo e ruído)
    # ==========================================================
    print("\n--- TESTE 1: Mono Misto (Fala sobreposta no mesmo canal) ---")
    t1_start = time.perf_counter()
    segments, info = model.transcribe(
        MONO_PATH,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400)
    )
    seg_list_1 = list(segments)
    t1_time = time.perf_counter() - t1_start

    audio_dur = 16.0
    speedup_1 = audio_dur / t1_time if t1_time > 0 else 0
    print(f"Tempo de Execução: {t1_time:.2f}s (Velocidade: {speedup_1:.1f}x tempo real)")
    print(f"Idioma Detectado: {info.language} ({info.language_probability*100:.1f}%)")
    print("\nTranscrição:")
    for s in seg_list_1:
        print(f"  [{s.start:.2f}s -> {s.end:.2f}s] {s.text}")

    # ==========================================================
    # TESTE 2: Dual-Channel Estéreo (Diarização por canal nativa)
    # ==========================================================
    print("\n--- TESTE 2: Dual-Channel Estéreo (Canal 0: Maria | Canal 1: David) ---")
    stereo_data, sr = sf.read(STEREO_PATH)
    ch0 = stereo_data[:, 0].astype(np.float32)
    ch1 = stereo_data[:, 1].astype(np.float32)

    t2_start = time.perf_counter()
    
    # Transcrever Canal 0 (Maria - PT)
    seg_ch0, info_ch0 = model.transcribe(
        ch0,
        language="pt",
        beam_size=5,
        vad_filter=True
    )
    list_ch0 = [(s.start, s.end, "Canal 0 (Maria - PT)", s.text) for s in seg_ch0]

    # Transcrever Canal 1 (David - EN)
    seg_ch1, info_ch1 = model.transcribe(
        ch1,
        language="en",
        beam_size=5,
        vad_filter=True
    )
    list_ch1 = [(s.start, s.end, "Canal 1 (David - EN)", s.text) for s in seg_ch1]

    # Intercalar cronologicamente os dois canais
    all_segments = sorted(list_ch0 + list_ch1, key=lambda x: x[0])
    t2_time = time.perf_counter() - t2_start
    speedup_2 = audio_dur / t2_time if t2_time > 0 else 0

    print(f"Tempo Total (Ambos os canais): {t2_time:.2f}s (Velocidade: {speedup_2:.1f}x tempo real)")
    print("\nTranscrição Diarizada Perfeita:")
    for start, end, speaker, text in all_segments:
        print(f"  [{start:.2f}s -> {end:.2f}s] [{speaker}]: {text}")

    peak_vram = torch.cuda.max_memory_allocated() / (1024**3)
    print(f"\n-> Pico de VRAM Utilizado: {peak_vram:.2f} GB")

if __name__ == "__main__":
    test_whisper()
