---
name: local-audio-transcription
description: Transcreve reuniões, chamadas e podcasts locais com alta performance utilizando faster-whisper (Large-v3-Turbo em CUDA FP16). Suporta áudios estéreo (dual-channel) com separação nativa de falantes, normalização de volume para vozes baixas e remoção de ruído via VAD. Use sempre que uma tarefa envolver transcrição, atas de reunião, extração de action items ou áudio.
---

# Local Audio Transcription (faster-whisper)

Esta skill orienta agentes e modelos de linguagem (Claude, Gemini, Grok, Qwen, Ollama) sobre como executar transcrições de áudio de reuniões localmente na GPU (RTX 4070) de forma headless (sem CLI, via biblioteca Python).

## 🚀 Quando Usar Esta Skill
- Transcrever reuniões, entrevistas ou podcasts gravados localmente.
- Processar áudios **estéreo / dual-channel** (onde um canal é o microfone local e o outro é o áudio remoto).
- Resolver áudios com **sobreposição de vozes**, ruídos de fundo e **diferenças acentuadas de volume**.
- Preparar atas de reunião estruturadas e extração de *action items*.

---

## 🛠️ Como Chamar o Módulo Localmente (Headless)

O serviço está implementado em `core/audio/transcriber.py` e gerencia o modelo na GPU em modo singleton (sem recarga desnecessária na VRAM).

### 1. Transcrição One-Shot de Reunião Estéreo (Dual-Channel)

```python
from core.audio.transcriber import transcriber

# Transcreve o arquivo com detecção automática de canais e normalização de volume
transcript = transcriber.transcribe_file(
    audio_path="caminho/para/reuniao.wav",
    speaker_0_label="Maria (Local)",
    speaker_1_label="Participantes (Remoto)"
)

# 1. Obter o texto formatado em Markdown pronto para o contexto da LLM:
print(transcript.to_markdown())

# 2. Obter metadados da execução:
print(f"Duração: {transcript.duration_sec}s | Tempo de GPU: {transcript.processing_time_sec}s ({transcript.realtime_factor}x tempo real)")
```

### 2. Transcrição Mono Simples

```python
from core.audio.transcriber import transcriber

transcript = transcriber.transcribe_file(
    audio_path="nota_de_voz.mp3",
    is_dual_channel=False,
    language="pt" # ou None para auto-detecção
)

print(transcript.full_text)
```

---

## 📋 Como Integrar o Resultado no Workflow da LLM

Quando uma LLM receber a tarefa de "gerar ata de reunião" ou "resumir discussão técnica":

```python
from core.audio.transcriber import transcriber
from core.router.model_router import query_ollama

# 1. Transcreve o áudio na GPU local (~23x mais rápido que tempo real)
transcript = transcriber.transcribe_file("meeting.wav")
markdown_notes = transcript.to_markdown()

# 2. Envia o markdown estruturado para uma LLM local (ex: qwen-code-deep ou gpt-oss-clean) ou nuvem
prompt = f"""Analise a seguinte transcrição de reunião e gere:
1. Resumo executivo das decisões tomadas.
2. Tabela de Action Items com responsável e prazo.
3. Principais divergências e pontos em aberto.

Transcrição:
{markdown_notes}
"""

# Exemplo de chamada local no Ollama:
# response = query_ollama("/api/generate", {"model": "qwen-code-deep:latest", "prompt": prompt})
```

---

## ⚙️ Diretrizes de Engenharia e Performance

1. **Hardware & VRAM**:
   - O modelo opera em `cuda` com `compute_type="float16"`.
   - Consumo de VRAM na RTX 4070: **~2.2 GB** (deixando >5.5 GB livres para modelos LLM do Ollama rodarem em paralelo).
2. **Normalização Automática**:
   - O método `transcribe_file` aplica normalização de ganho individual por canal. Se um participante estiver com microfone muito baixo, o volume é elevado automaticamente antes de entrar no Whisper.
3. **Filtro VAD (Voice Activity Detection)**:
   - Mantido como `vad_filter=True` por padrão para ignorar silêncios longos e ruídos de respiração/cliques.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **RCA de Falhas em Áudio (Clipping, Sample Rate, Canais)**:
   - Falhas comuns (áudios mono tratados como estéreo, arquivos corrompidos, VAD cortando início de frases baixas) são diagnosticadas via 5-Whys.
   - Sempre que um usuário reclamar de perda de fala ou atraso, registre no RCA (`python core/learning/cli.py rca`) e ajuste os limiares de VAD e ganho estéreo para que se tornem o padrão do módulo.
2. **One-Shot em Tarefas de Ata e Resumo**:
   - Ao transcrever, gere o Markdown de notas com sumário executivo e action items estruturados na primeira passagem, evitando que o usuário precise pedir formatações adicionais.
3. **Extrapolação de Performance GPU**:
   - As lições de gerenciamento de VRAM na RTX 4070 (isolamento singleton, libertação de memória) devem ser transferidas para qualquer outro módulo local de inferência ou modelos de visão.

