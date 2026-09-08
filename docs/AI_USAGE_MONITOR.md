# AI Account Monitor e Model Ledger

## Objetivo

O Hub mostra, em um único painel, o estado das contas de IA e o consumo percentual das janelas que cada plataforma realmente publica. A mesma feature mantém um ledger do uso de modelos do projeto, agregado por provedor, modelo, tier, harness e modalidade.

## Semântica de conta

- `connected`: a sessão, credencial ou serviço local foi validado.
- `limited`: existe evidência de quota esgotada.
- `disconnected`: a conta não está configurada. É um estado normal e a API continua respondendo `200`.
- `degraded`: a plataforma foi detectada, mas o probe falhou isoladamente.
- Percentual `null` significa “não publicado”; nunca é convertido em `0%`.

- OpenAI/Codex: localiza o executável `codex.exe` (no PATH, em `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`, ou em `~/.codex/`) e conecta-se ao `codex app-server --listen stdio://` via JSON-RPC (`account/rateLimits/read`), extraindo percentuais reais de uso e timestamps de reset para a janela de 5 horas (`primary`) e janela semanal (`secondary`).
- Google / Gemini & Antigravity: descobre dinamicamente a porta HTTPS local e o token de segurança (`--csrf_token`) do Antigravity Language Server ativo (em `AppData\Roaming\Antigravity\logs\main.log`) e consulta via ConnectRPC os endpoints `/GetAvailableModels` e `/GetUserStatus`, obtendo o percentual real de consumo da janela de 5h e o plano ativo (`Pro`).
- xAI / Grok: descobre a sessão autenticada do Grok Bot em `AppData\Roaming\Grok Bot`, decriptando o token de acesso protegido por DPAPI (`CryptUnprotectData`) e chave mestra AES-256-GCM de `Local State` e `sand-secrets.json`, e consulta o endpoint ConnectRPC `GetSandUsageStatus` em `api2.cursor.sh` para obter o consumo percentual real do pool semanal compartilhado (`SuperGrok`) e a data exata de reset. Caso o Grok Bot não esteja configurado, utiliza fallback autenticado via CLI (`~/.grok/auth.json`) ou API keys.

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

