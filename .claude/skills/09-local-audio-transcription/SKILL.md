---
name: local-audio-transcription
description: Transcreve reuniões, chamadas e podcasts locais com alta performance utilizando faster-whisper (Large-v3-Turbo em CUDA FP16). Suporta áudios estéreo (dual-channel) com separação nativa de falantes, normalização de volume para vozes baixas e remoção de ruído via VAD. Use sempre que uma tarefa envolver transcrição, atas de reunião, extração de action items ou áudio.
---

# 09 - Local Audio Transcription (faster-whisper)

Esta skill orienta a execução de transcrições de áudio locais de alta velocidade e fidelidade na GPU (RTX 4070) de forma headless (via biblioteca Python), gerando notas estruturadas e action items com custo financeiro zero.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Arquivo de Áudio**: Caminho absoluto para gravação local (`.wav`, `.mp3`, `.m4a`), mono ou estéreo (dual-channel).
- **Rótulos de Falantes**: Identificadores opcionais (`speaker_0_label`, `speaker_1_label`).
- **Parâmetros de Detecção**: Idioma (ex: `"pt"`, `"en"` ou `None` para detecção automática) e flag VAD.

### Ações e Procedimento Executável
1. **Invocação Headless via Biblioteca**:
   O serviço reside em `core/audio/transcriber.py` e gerencia o modelo na GPU em modo singleton:
   ```python
   from core.audio.transcriber import transcriber

   # Transcrição estéreo / dual-channel com normalização automática de ganho
   transcript = transcriber.transcribe_file(
       audio_path="caminho/para/reuniao.wav",
       speaker_0_label="Maria (Local)",
       speaker_1_label="Participantes (Remoto)"
   )
   ```
2. **Transcrição Mono Simples**:
   ```python
   transcript = transcriber.transcribe_file(
       audio_path="nota_de_voz.mp3",
       is_dual_channel=False,
       language="pt"
   )
   ```
3. **Pós-Processamento e Extração de Action Items**:
   Formate o texto em Markdown e submeta a um modelo local ou econômico para gerar:
   - Resumo executivo das decisões.
   - Tabela estruturada de tarefas e responsáveis.
   - Pontos de divergência ou itens em aberto.

### Outputs Estruturados
- **Objeto `TranscriptResult`**:
  - `to_markdown()`: Diálogo formatado com timestamps e falantes identificados.
  - `full_text`: Texto corrido integral.
  - `duration_sec`: Duração do áudio original.
  - `processing_time_sec` e `realtime_factor`: Métricas de telemetria de processamento.
- **Artefato de Ata**: Arquivo Markdown persistido para consumo de agentes ou usuários.

### Portões, Política e Validação
- **Desacoplamento Headless**: A chamada deve operar como biblioteca pura ou script CLI sem exigir servidor visual ativo.
- **Privacidade e Segurança**: Áudios e transcrições permanecem locais; proibido envio de dados confidenciais a APIs públicas não autorizadas.
- **Eficiência de VRAM**: Modelo fixado em CUDA FP16 consumindo ~2.2 GB de VRAM, preservando recursos para modelos LLM do Ollama em paralelo.

---

## 2. Continuous Self-Improvement & RCA de Áudio

- **RCA em Problemas de Áudio**: Se ocorrer corte de fala baixa por VAD ou desbalanceamento entre canais, ajuste os parâmetros de normalização no transcriber e registre a causa raiz via `python -m core.learning.cli rca`.
