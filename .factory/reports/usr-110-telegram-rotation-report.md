# Relatório de Conclusão: USR-110

**Ticket**: USR-110  
**Título**: Telegram: webhooks ausentes apos rotacao de token, tokens locais obsoletos e falhas silenciosas no poller/registro  
**Status**: `completed`  
**Data**: 2026-10-05  

---

## 1. Contexto e Problema

Após a rotação de tokens motivada pelo USR-100, verificou-se que comandos enviados aos bots `@darkfac_bot` (owner) e `@darkfac_ops_bot` (ops) não obtinham resposta. O diagnóstico revelou:
1. `core.integrations.telegram_webhooks.register_telegram_webhooks` falhava silenciosamente ou com log apenas em nível `warning`, sem expor o erro e o status por papel em `/api/integrations/telegram/status`;
2. Em ambientes locais (`.env`, `.factory/telegram/owner_config.json`, `ops_config.json`), credenciais revogadas (HTTP 401) passavam despercebidas pois `run_hub.py` (`start_telegram_listener`) engolia exceções (`except Exception: pass`) e `TelegramGateway.poll_updates` emitia apenas avisos genéricos com potencial vazamento de token na URL do erro;
3. Não havia mecanismo para detectar divergências entre as fontes locais de token (`env`, `.env`, `owner_config.json`, `ops_config.json`, `config.json`);
4. Faltava uma ferramenta CLI de conferência sem vazamento de segredos e um runbook consolidado de rotação com validação ponta a ponta.

---

## 2. Solução Implementada

1. **Rastreamento de Registro de Webhook (`core/integrations/telegram_webhooks.py`)**:
   - Criada a função `get_webhook_registration_status()` que expõe, por papel, a URL registrada, o status (`updated`, `unchanged`, `failed`) e o último erro sem expor tokens (apenas código HTTP ou tipo de exceção).
   - O log de falha no registro de webhook foi elevado para `logger.error`.
   - Em `hub/backend/main.py` (`_lifespan`), falhas de registro no startup agora emitem `_logger_hub.error`.

2. **Visibilidade no DarkHub (`hub/backend/service.py`)**:
   - `HubService.get_telegram_gateway_status()` agora anexa a chave `webhooks` com o relatório completo emitido por `get_webhook_registration_status()`, consumível via `GET /api/integrations/telegram/status`.

3. **Robustez e Fim do Silêncio em Erros HTTP 401 e 409 (`core/integrations/telegram.py`, `run_hub.py`)**:
   - Em `TelegramGateway.poll_updates`, erros de rede não vazam a URL com o token no log. Códigos HTTP 401 (Unauthorized - token inválido/revogado) e HTTP 409 (Conflict - webhook ativo ou concorrência) são logados explicitamente com `logger.error`.
   - Em `run_hub.py` (`start_telegram_listener`), removido o bloco `except Exception: pass`. Falhas no loop do poller emitem avisos com identificação do status HTTP a cada 30 segundos, sem interrupção abrupta da aplicação.

4. **Detecção de Divergência de Fontes de Token (`core/integrations/telegram.py`)**:
   - Em `load_telegram_config`, todas as fontes locais são comparadas (`os.environ`, `.env`, arquivos JSON). Caso contenham tokens diferentes para o mesmo papel, é emitido um alerta estruturado `logger.warning("Telegram token sources diverge for role=%s: ...")` exibindo somente fingerprints mascaradas (`1234...abcd`), nunca tokens completos.

5. **Ferramenta de Verificação Automatizada (`scripts/telegram_rotate_check.py`)**:
   - Novo script executável via linha de comando (`python scripts/telegram_rotate_check.py` ou `--json`).
   - Varre todas as 6 fontes locais de token, testa `getMe` e `getWebhookInfo` perante a API oficial do Telegram e imprime uma tabela de diagnóstico com fingerprints mascaradas e diagnóstico limpo.

6. **Runbook de Rotação (`docs/runbooks/telegram_rotation.md`)**:
   - Guia passo a passo tela por tela para revogação e criação de novos tokens no `@BotFather`, atualização no Dokploy (produção) e no `.env` local, execução do script de conferência e teste de validação `/start`.

---

## 3. Evidências de Teste e Validação

- `tests/test_telegram_rotation_usr110.py`: 7/7 testes aprovados cobrindo mascaramento, parsing de `.env`, captura de status e log de erro de webhook, aviso de divergência de token, tratamento de HTTP 401/409 e sanitização no script de conferência.
- `tests/test_telegram_dual_bot.py` e `tests/test_line_bots_wakeups.py`: 34/34 testes conexos aprovados sem regressões.
- `scripts/hub_coverage.py`: 100% de cobertura de superfícies mantida.
- `scripts/telegram_rotate_check.py`: validado em execução real com diagnóstico preciso das credenciais locais.
