# AI Account Monitor e Model Ledger

## Objetivo

O Hub mostra, em um único painel, o estado das contas de IA e o consumo percentual das janelas que cada plataforma realmente publica. A mesma feature mantém um ledger do uso de modelos do projeto, agregado por provedor, modelo, tier, harness e modalidade.

## Semântica de conta

- `connected`: a sessão, credencial ou serviço local foi validado.
- `limited`: existe evidência de quota esgotada.
- `disconnected`: a conta não está configurada. É um estado normal e a API continua respondendo `200`.
- `degraded`: a plataforma foi detectada, mas o probe falhou isoladamente.
- Percentual `null` significa “não publicado”; nunca é convertido em `0%`.

OpenAI/Codex usa o app-server oficial instalado para ler buckets móveis, incluindo as janelas de 5 horas e semanal. Grok valida a sessão pelo CLI; a quota do plano de consumidor não é publicada por esse CLI. Gemini valida Antigravity ou API key e registra `100%`/reset quando encontra um `RESOURCE_EXHAUSTED` estruturado; fora disso, mantém o percentual desconhecido. Essa diferença é deliberada: conexão comprovada não implica telemetria de assinatura disponível.

Além das três contas prioritárias, o registro inclui Anthropic, OpenRouter, DeepSeek, SiliconFlow, Qwen, Moonshot/Kimi, Zhipu/GLM, MiniMax e Ollama. Novos provedores implementam `AccountUsageAdapter` e são adicionados por `build_default_adapters`.

## Snapshots de quota

Quando uma plataforma não oferece uma API local estável, um harness autorizado pode publicar um snapshot sanitizado em `.factory/usage/providers/<provider_id>.json` ou definir `DARKFAC_<PROVIDER_ID>_USAGE_JSON` com o caminho/JSON:

```json
{
  "status": "connected",
  "plan": "pro",
  "account_label": "conta pessoal",
  "windows": [
    {
      "id": "rolling-5h",
      "label": "5h",
      "used_percent": 42,
      "window_duration_minutes": 300,
      "resets_at": "2026-09-05T18:00:00Z"
    }
  ]
}
```

Não grave cookies, tokens, API keys ou payloads brutos de conta. O monitor só persiste valores sanitizados.

## Model Ledger

As chamadas do Hub, `ContentEngine`, `VisualStudio` e router local são registradas no ponto de I/O real. Harnesses externos usam a API ou CLI:

```powershell
python -m core.usage.cli record `
  --invocation-id grok-run-42 `
  --provider xai `
  --model grok-4.6 `
  --tier frontier `
  --harness grok-build `
  --modality text
```

`invocation_id` torna retries idempotentes. O ledger é fail-open: falha de observabilidade não interrompe geração. Ele guarda no máximo 500 eventos recentes e preserva os agregados históricos em `.factory/usage/model_usage.json`.

## API headless

- `GET /api/usage/accounts`: leitura com cache curto.
- `POST /api/usage/accounts/refresh`: força probes.
- `GET /api/usage/models`: totais e agregados.
- `POST /api/usage/models/events`: ingestão sanitizada de qualquer harness; fica desabilitado sem `DARKFAC_TELEMETRY_KEY` e exige o header `X-DarkFac-Telemetry-Key`.

CLI equivalente:

```powershell
python -m core.usage.cli accounts --refresh
python -m core.usage.cli models
```

## Non-goals

- Raspar cookies ou páginas privadas de consumidor.
- Inventar quota ou percentual a partir de presença de API key.
- Unificar créditos monetários, RPM/TPM e limites móveis como se fossem a mesma métrica.
- Tornar inferência dependente do monitor.

## Referências de produto

- OpenAI Codex: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- Gemini rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- xAI Management API: https://docs.x.ai/docs/key-information/management-api

