#!/usr/bin/env python3
"""
Gera áudios de teste simulando reunião real:
- Dois canais (estéreo)
- Fala sobreposta (overlapping speech)
- Discrepância de volume (Maria alta, David baixo)
- Ruído de fundo (ruído rosa / ruído ambiente de sala)
"""

import os
import soundfile as sf
import numpy as np

AUDIO_DIR = os.path.join(os.path.dirname(__file__))

def create_meeting_audio():
    maria, sr_m = sf.read(os.path.join(AUDIO_DIR, "maria_pt.wav"))
    david, sr_d = sf.read(os.path.join(AUDIO_DIR, "david_en.wav"))

    target_sr = 16000
    if sr_m != target_sr:
        from scipy import signal
        maria = signal.resample_poly(maria, target_sr, sr_m)
    if sr_d != target_sr:
        from scipy import signal
        david = signal.resample_poly(david, target_sr, sr_d)

    # Converter para mono 1D se necessário
    if maria.ndim > 1:
        maria = maria.mean(axis=1)
    if david.ndim > 1:
        david = david.mean(axis=1)

    # Normalizar Maria para pico 0.95
    maria = maria / (np.max(np.abs(maria)) + 1e-6) * 0.95

    # David com volume baixo (diferença de volume realista: 45% do volume de Maria)
    david = david / (np.max(np.abs(david)) + 1e-6) * 0.45

    # Duração total: Maria (8s) + sobreposição com David a partir de t=5.0s
    total_samples = int(target_sr * 16.0)

    ch_left = np.zeros(total_samples, dtype=np.float32)
    ch_right = np.zeros(total_samples, dtype=np.float32)

    # Maria fala no Canal Esquerdo (Left) a partir de 0.5s
    m_start = int(target_sr * 0.5)
    m_len = min(len(maria), total_samples - m_start)
    ch_left[m_start:m_start + m_len] += maria[:m_len]

    # David fala no Canal Direito (Right) a partir de 4.5s (sobrepondo 3.5s com Maria)
    d_start = int(target_sr * 4.5)
    d_len = min(len(david), total_samples - d_start)
    ch_right[d_start:d_start + d_len] += david[:d_len]

    # Adicionar ruído de fundo realista (-30 dB)
    np.random.seed(42)
    noise_left = np.random.normal(0, 0.015, total_samples).astype(np.float32)
    noise_right = np.random.normal(0, 0.015, total_samples).astype(np.float32)

    ch_left += noise_left
    ch_right += noise_right

    # Salvar Estéreo (Dois canais separados)
    stereo = np.stack([ch_left, ch_right], axis=1)
    stereo_path = os.path.join(AUDIO_DIR, "meeting_stereo.wav")
    sf.write(stereo_path, stereo, target_sr)
    print(f"Salvo áudio estéreo 2 canais: {stereo_path} ({len(stereo)/target_sr:.1f}s)")

    # Salvar Mono Misto (Canais somados, fala sobreposta no mesmo canal)
    mono = (ch_left + ch_right) * 0.6
    mono_path = os.path.join(AUDIO_DIR, "meeting_mono_mixed.wav")
    sf.write(mono_path, mono, target_sr)
    print(f"Salvo áudio mono misto com sobreposição: {mono_path} ({len(mono)/target_sr:.1f}s)")

if __name__ == "__main__":
    create_meeting_audio()
