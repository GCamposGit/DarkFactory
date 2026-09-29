/**
 * DarkHub - High-Speed Web Audio Capture & Transcription Client (USR-60).
 *
 * Captures microphone audio using native browser Web Audio API in 16kHz Mono PCM,
 * encodes directly to a pure 16-bit WAV Blob without requiring external FFmpeg or WASM codecs,
 * and streams to /api/audio/transcribe (hybrid faster-whisper + Groq Cloud fallback).
 */

const darkHubAudio = {
  isRecording: false,
  audioContext: null,
  mediaStream: null,
  audioInput: null,
  scriptProcessor: null,
  recordedPcmBuffers: [],
  activeQuestionId: null,
};

function encode16BitWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  function writeString(view, offset, string) {
    for (let i = 0; i < string.length; i++) {
      view.setUint8(offset + i, string.charCodeAt(i));
    }
  }

  /* RIFF chunk */
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(view, 8, 'WAVE');

  /* fmt chunk */
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true); // SubChunk1Size (16 for PCM)
  view.setUint16(20, 1, true);  // AudioFormat (1 for PCM)
  view.setUint16(22, 1, true);  // NumChannels (1 mono)
  view.setUint32(24, sampleRate, true); // SampleRate
  view.setUint32(28, sampleRate * 2, true); // ByteRate (SampleRate * NumChannels * BitsPerSample/8)
  view.setUint16(32, 2, true);  // BlockAlign
  view.setUint16(34, 16, true); // BitsPerSample

  /* data chunk */
  writeString(view, 36, 'data');
  view.setUint32(40, samples.length * 2, true);

  // Write 16-bit PCM samples
  let index = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(index, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    index += 2;
  }

  return new Blob([buffer], { type: 'audio/wav' });
}

async function startAudioCapture() {
  if (darkHubAudio.isRecording) return false;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
    });
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx({ sampleRate: 16000 });
    const input = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);

    darkHubAudio.recordedPcmBuffers = [];
    processor.onaudioprocess = (e) => {
      if (!darkHubAudio.isRecording) return;
      const channelData = e.inputBuffer.getChannelData(0);
      darkHubAudio.recordedPcmBuffers.push(new Float32Array(channelData));
    };

    input.connect(processor);
    processor.connect(ctx.destination);

    darkHubAudio.mediaStream = stream;
    darkHubAudio.audioContext = ctx;
    darkHubAudio.audioInput = input;
    darkHubAudio.scriptProcessor = processor;
    darkHubAudio.isRecording = true;
    return true;
  } catch (err) {
    console.error('Microphone access failed:', err);
    if (typeof showToast === 'function') {
      showToast('Permissão de microfone negada ou indisponível.', 'error');
    }
    return false;
  }
}

async function stopAudioCapture() {
  if (!darkHubAudio.isRecording) return null;
  darkHubAudio.isRecording = false;

  if (darkHubAudio.scriptProcessor) {
    darkHubAudio.scriptProcessor.disconnect();
    darkHubAudio.scriptProcessor = null;
  }
  if (darkHubAudio.audioInput) {
    darkHubAudio.audioInput.disconnect();
    darkHubAudio.audioInput = null;
  }
  if (darkHubAudio.audioContext) {
    await darkHubAudio.audioContext.close().catch(() => {});
    darkHubAudio.audioContext = null;
  }
  if (darkHubAudio.mediaStream) {
    darkHubAudio.mediaStream.getTracks().forEach((t) => t.stop());
    darkHubAudio.mediaStream = null;
  }

  if (darkHubAudio.recordedPcmBuffers.length === 0) return null;

  const totalLength = darkHubAudio.recordedPcmBuffers.reduce((acc, b) => acc + b.length, 0);
  const mergedSamples = new Float32Array(totalLength);
  let offset = 0;
  for (const buf of darkHubAudio.recordedPcmBuffers) {
    mergedSamples.set(buf, offset);
    offset += buf.length;
  }
  darkHubAudio.recordedPcmBuffers = [];

  return encode16BitWav(mergedSamples, 16000);
}

async function uploadAudioForTranscription(wavBlob, language = 'pt') {
  const request = typeof hubFetch === 'function' ? hubFetch : fetch;
  const res = await request(`/api/audio/transcribe?language=${encodeURIComponent(language)}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'audio/wav',
    },
    body: wavBlob,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}: Erro na transcrição de áudio`);
  }

  return await res.json();
}

/**
 * Toggles voice recording for a specific question inside the Grill Modal.
 * Fills the custom input field with the transcribed text and auto-selects corresponding radio option.
 */
async function toggleGrillQuestionVoice(questionId) {
  const btn = document.getElementById(`btn-voice-q-${questionId}`);
  const statusEl = document.getElementById(`status-voice-q-${questionId}`);
  const inputEl = document.getElementById(`custom_q_${questionId}`);

  if (darkHubAudio.isRecording) {
    if (darkHubAudio.activeQuestionId !== questionId) {
      if (typeof showToast === 'function') {
        showToast('Finalize a gravação da questão anterior primeiro.', 'warning');
      }
      return;
    }

    // Stop recording and transcribe
    if (btn) {
      btn.classList.remove('bg-rose-600', 'text-white', 'animate-pulse');
      btn.classList.add('bg-slate-800');
      btn.innerHTML = '<span>⏳</span><span class="text-[11px] hidden sm:inline">Transcrevendo...</span>';
      btn.disabled = true;
    }
    if (statusEl) {
      statusEl.classList.remove('hidden');
      statusEl.textContent = 'Transcrevendo áudio via motor híbrido ($0 / Groq)...';
    }

    try {
      const wavBlob = await stopAudioCapture();
      if (!wavBlob || wavBlob.size < 1000) {
        if (statusEl) statusEl.textContent = 'Áudio muito curto ou vazio. Tente novamente.';
        return;
      }

      const result = await uploadAudioForTranscription(wavBlob, 'pt');
      const text = (result.text || '').trim();

      if (!text) {
        if (statusEl) statusEl.textContent = 'Nenhuma fala audível detectada.';
        return;
      }

      // Populate text input
      if (inputEl) {
        inputEl.value = text;
        inputEl.focus();
      }

      // Auto-selection heuristic (Gate G1 approved)
      let optionMatched = false;
      const session = interventionsState?.activeGrillSession;
      if (session) {
        const q = (session.questions || []).find((item) => String(item.id) === String(questionId));
        if (q && q.options) {
          const lowerText = text.toLowerCase();
          const radios = document.getElementsByName(`grill_q_${q.id}`);

          q.options.forEach((opt, idx) => {
            const optLabel = (opt.label || '').toLowerCase();
            const isRec = Boolean(opt.is_recommended);
            const matchesText = optLabel && lowerText.includes(optLabel);
            const matchesFirst = idx === 0 && (lowerText.includes('primeira') || lowerText.includes('opção 1') || lowerText.includes('opcao 1'));
            const matchesSecond = idx === 1 && (lowerText.includes('segunda') || lowerText.includes('opção 2') || lowerText.includes('opcao 2'));
            const matchesThird = idx === 2 && (lowerText.includes('terceira') || lowerText.includes('opção 3') || lowerText.includes('opcao 3'));
            const matchesRec = isRec && (lowerText.includes('recomendada') || lowerText.includes('recomendado') || lowerText.includes('padrão'));

            if (matchesText || matchesFirst || matchesSecond || matchesThird || matchesRec) {
              if (radios) {
                for (const r of radios) {
                  if (r.value === opt.label) {
                    r.checked = true;
                    optionMatched = true;
                    break;
                  }
                }
              }
            }
          });
        }
      }

      if (statusEl) {
        const feedback = optionMatched
          ? `✓ Transcrito (${result.engine_used}): "${text}" (Opção correspondente auto-selecionada)`
          : `✓ Transcrito (${result.engine_used}): "${text}"`;
        statusEl.textContent = feedback;
        statusEl.classList.remove('text-amber-400');
        statusEl.classList.add('text-emerald-400');
      }
      if (typeof showToast === 'function') {
        showToast('Resposta de áudio transcrita com sucesso!', 'success');
      }
    } catch (err) {
      console.error('Transcription error:', err);
      if (statusEl) {
        statusEl.textContent = `Erro na transcrição: ${err.message}`;
        statusEl.classList.remove('text-emerald-400');
        statusEl.classList.add('text-rose-400');
      }
    } finally {
      darkHubAudio.activeQuestionId = null;
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>🎙️</span><span class="text-[11px] hidden sm:inline">Voz</span>';
      }
    }
  } else {
    // Start recording
    const started = await startAudioCapture();
    if (!started) return;

    darkHubAudio.activeQuestionId = questionId;
    if (btn) {
      btn.classList.remove('bg-slate-800');
      btn.classList.add('bg-rose-600', 'text-white', 'animate-pulse');
      btn.innerHTML = '<span>⏹️</span><span class="text-[11px] hidden sm:inline">Parar</span>';
    }
    if (statusEl) {
      statusEl.classList.remove('hidden', 'text-rose-400', 'text-emerald-400');
      statusEl.classList.add('text-amber-400');
      statusEl.textContent = 'Gravando resposta... Clique em Parar para transcrever.';
    }
  }
}

/**
 * Toggles voice recording for the New Demand Drawer problem/title field.
 */
async function toggleDemandDrawerVoice(targetFieldId = 'demand-problem-input') {
  const btn = document.getElementById('btn-voice-demand-drawer');
  const target = document.getElementById(targetFieldId);

  if (darkHubAudio.isRecording) {
    if (btn) {
      btn.classList.remove('bg-rose-600', 'text-white', 'animate-pulse');
      btn.classList.add('bg-slate-800');
      btn.innerHTML = '<span>⏳</span> Transcrevendo...';
      btn.disabled = true;
    }

    try {
      const wavBlob = await stopAudioCapture();
      if (wavBlob && wavBlob.size >= 1000) {
        const result = await uploadAudioForTranscription(wavBlob, 'pt');
        const text = (result.text || '').trim();
        if (text && target) {
          target.value = target.value ? `${target.value}\n${text}` : text;
          target.focus();
        }
        if (typeof showToast === 'function') {
          showToast(`Áudio transcrito via ${result.engine_used}!`, 'success');
        }
      }
    } catch (err) {
      console.error('Demand drawer voice error:', err);
      if (typeof showToast === 'function') showToast(`Erro no áudio: ${err.message}`, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>🎙️</span> Ditar Demanda por Voz';
      }
    }
  } else {
    const started = await startAudioCapture();
    if (!started) return;
    if (btn) {
      btn.classList.remove('bg-slate-800');
      btn.classList.add('bg-rose-600', 'text-white', 'animate-pulse');
      btn.innerHTML = '<span>⏹️</span> Parar Gravação';
    }
    if (typeof showToast === 'function') {
      showToast('Gravando áudio da demanda... Fale seu problema ou funcionalidade.', 'info');
    }
  }
}
