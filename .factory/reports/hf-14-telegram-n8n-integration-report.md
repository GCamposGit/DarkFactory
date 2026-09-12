# Relatório de Conclusão: HF-14 — Telegram do Owner e n8n Community Simples, com Autenticação, Deduplicação e Workflows Sanitizados

- **Ticket**: `HF-14`
- **Data**: 11/09/2026
- **Status**: `COMPLETED`
- **Governança**: `HYBRID_WORKFLOW_PLAN_2026-09-08` (Seções 5, 8, 9, 10, linhas 266, 299, 300, 303, 305) e `HYBRID_AUTONOMY_REQUIREMENTS` (Seções 4, 7, Cenários G1, G5 e G8)
- **Reúso / Complemento**: `USR-18`, `INFRA-09`, `DF-21`, `HF-08`, `HF-12`, `HF-13`, Skills 04, 05 e 07
- **Módulos Afetados**: `core.integrations.telegram`, `core.integrations.n8n`, `core.integrations.telegram_n8n_cli`, `hub.backend.api`, `hub.backend.service`, `tests/test_hf14_telegram_n8n_integration.py`, `docs/handoffs/HF-14.md`

---

## 1. Contexto e Objetivos

O ticket **HF-14** conecta o proprietário humano à esteira autônoma de forma móvel e prática via **Telegram** (intake de demandas, acompanhamento de status, respostas a perguntas do Grill e aprovação de releases) e estrutura o ecossistema de **n8n Community** auto-hospedado (Docker Compose, PostgreSQL dedicado, verificação técnica de instâncias reais e exportação de workflows sanitizados).

Anteriormente, o sistema necessitava de um canal direto, seguro e de baixo atrito para comandos e decisões do proprietário em mobilidade, além de uma definição técnica clara para a automação de workflows no n8n sem incorrer em custos de licenças adicionais.

O HF-14 consolida:
1. **Autenticação e Pareamento Exclusivo**: Whitelist fail-closed de IDs de usuário e chat do Telegram.
2. **Deduplicação e Offset Durável (Cenário G5)**: Idempotência de mensagens e callbacks, prevenindo reexecução de comandos em retransmissões de rede.
3. **Retomada Durável de Runs (Cenários G1 e G8)**: Resolução de Grill e aprovações de release em mobilidade.
4. **Resiliência e Desacoplamento do Telegram**: Falhas de rede ou na API do Telegram não derrubam o pipeline; outbox local armazena pendências.
5. **Sanitização de Segredos**: Varredura por expressões regulares mascarando credenciais e tokens em mensagens e logs.
6. **n8n Community Self-Hosted ($0 em novas licenças)**: Manifesto Docker Compose com PostgreSQL 16 isolado, probe de integridade (`N8nProbe`) e sanitizador de workflows em Git.

---

## 2. Entregas e Invariantes Comprovadas

### 2.1. Gateway e Adaptador Telegram (`core.integrations.telegram`)
- `TelegramGateway` gerencia autenticação, offset e deduplicação de mensagens e callbacks.
- Rastreamento de `last_offset` em arquivo atômico `gateway_state.json`.
- `redact_secrets` assegura que tokens (`bot*`, `ghp_*`, `sk-*`, senhas) sejam substituídos por `[REDACTED_SECRET]`.
- Resiliência contra quedas da Bot API via outbox local em `outbox.json`.

### 2.2. Automação e Sonda n8n Community (`core.integrations.n8n`)
- `N8nProbe` detecta `https://n8n.io` como placeholder genérico da documentação (`is_generic_placeholder=True`) e executa checagens ativas contra `/healthz` de instâncias reais.
- `N8nManifestGenerator` emite Docker Compose oficial com serviço `n8n` (`n8nio/n8n`), `postgres` (`postgres:16-alpine`), volumes nomeados e configurações de retenção.
- `N8nWorkflowManager.export_workflow` limpa credenciais e dados sensíveis antes de salvar arquivos JSON em Git.

### 2.3. Workflows Referenciais e Manifestos
- `.factory/n8n/docker-compose.n8n.yml`: Docker Compose pronto para Dokploy / VPS.
- `.factory/n8n/workflows/telegram_webhook_gateway.json`: Webhook gateway n8n -> DarkHub.
- `.factory/n8n/workflows/pipeline_alerts.json`: Alertas de incidentes DarkHub -> Telegram.

### 2.4. CLI Headless Operacional (`core.integrations.telegram_n8n_cli`)
- Subcomandos de linha de comando com flag `--json` e saída UTF-8 no Windows.

### 2.5. Integração com DarkHub (`hub.backend.api` e `hub.backend.service`)
- Endpoints REST para webhooks do Telegram (`/api/webhooks/telegram`), status do gateway (`/api/integrations/telegram/status`) e sonda do n8n (`/api/integrations/n8n/status`).

---

## 3. Matriz de Testes e Evidências

| Teste | Escopo / Invariante | Resultado |
|---|---|---|
| `test_telegram_auth_accepts_authorized_user` | Usuário autorizado é aceito para interação com DarkFac | PASSED |
| `test_telegram_auth_rejects_unauthorized_user` | Usuário não autorizado é sumariamente rejeitado (fail-closed) | PASSED |
| `test_telegram_deduplication_ignores_duplicate_update_id` | Cenário G5: update duplicado é ignorado sem reexecutar handlers | PASSED |
| `test_telegram_durable_offset_advances_and_persists` | Offset persiste em disco e sobrevive a reinicialização | PASSED |
| `test_telegram_demand_command_creates_ticket` | Comando `/demand` registra ticket no backlog e devolve ID | PASSED |
| `test_telegram_grill_callback_resumes_waiting_human_run` | Cenário G1: resposta de Grill via callback retoma o run | PASSED |
| `test_telegram_approve_release_records_acceptance` | Cenário G8: aprovação de release emite recibo e autoriza produção | PASSED |
| `test_telegram_network_outage_does_not_fail_workflow` | Indisponibilidade do Telegram enfileira notificação no outbox | PASSED |
| `test_telegram_message_sanitizes_secrets` | Tokens e senhas são mascarados com [REDACTED_SECRET] | PASSED |
| `test_n8n_probe_distinguishes_generic_link_from_real_instance` | Distingue https://n8n.io de instâncias reais com /healthz | PASSED |
| `test_n8n_manifest_generator_emits_valid_compose` | Gera Docker Compose com n8n Community, Postgres 16 e volumes | PASSED |
| `test_n8n_workflow_sanitizer_redacts_credentials` | Exportação remove credenciais de nós preservando grafo | PASSED |
| `test_hub_endpoints_telegram_and_n8n` | Endpoints REST no DarkHub para Telegram e n8n funcionam | PASSED |
| `test_cli_headless_telegram_and_n8n_commands` | Subcomandos CLI operam deterministicamente com flag --json | PASSED |

---

## 4. Validação do Portão Oficial

- **Comando**: `python core/harness/runner.py --quick`
- **Total de Testes**: **806 descobertos** (804 aprovados, 2 skipped, 0 falhas)
- **Status do Portão**: **`[HARNESS_PASS]`**
- **Exit Code**: `0`

---

## 5. Próximo Sucessor

O próximo ticket na sequência do Plano Híbrido é **HF-15** (Ensaio completo de aceitação e documentação de operação da fábrica autônoma — encerramento da Onda 1).
