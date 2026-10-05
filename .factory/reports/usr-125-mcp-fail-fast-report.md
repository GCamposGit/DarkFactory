# Relatório de Conclusão: USR-125

**Ticket**: USR-125  
**Título**: Fail fast when optional Segundo Cérebro MCP is unavailable  
**Status**: `completed`  
**Data**: 2026-10-05  
**PR**: [#153](https://github.com/GCamposGit/DarkFactory/pull/153) (commit `876e334`)  
**Deploy Dokploy VPS**: Convergido (`876e334`), backup drill verificado (`snp3t_darkfac_20261005_141448_4a3d94`)  

---

## 1. Contexto e Problema

Durante a execução do USR-124, a inicialização do MCP opcional do Segundo Cérebro em `http://127.0.0.1:18788/mcp` falhou (porta não aberta/servidor local desligado). O harness headless (Claude Code) tentou estabelecer o handshake de inicialização do MCP e, sem timeout estrito na camada de transporte/integração opcional, aguardou até o timeout total do agente de 1.800 segundos (30 minutos) antes de abortar. Uma dependência externa puramente auxiliar e opcional consumiu 30 minutos de máquina e impediu que o ticket chegasse à entrega.

---

## 2. Solução Implementada

1. **Preflight e Fail-Fast para Servidores MCP em `core/line/agent_cli.py`**:
   - Implementada a rotina `check_optional_mcp_servers(config_path, timeout_sec=2.0)`.
   - Realiza probe de conectividade de socket TCP direto contra qualquer servidor MCP do tipo HTTP/SSE configurado em `~/.claude.json` ou configs do ambiente.
   - Timeout determinístico e curto de no máximo 2,0s (`DEFAULT_MCP_PROBE_TIMEOUT`).
   - Se o servidor estiver indisponível (conexão recusada ou timeout), o estado degradado é registrado e a flag `--strict-mcp-config` é acionada automaticamente via `build_claude_argv`.
   - `--strict-mcp-config` instrui o Claude Code CLI a desconsiderar servidores MCP externos não informados na chamada, permitindo que a sessão inicie instantaneamente em ~9s em vez de travar por 1.800s.

2. **Resiliência e Modo Degradado no `SegundoCerebroClient` (`core/knowledge/segundo_cerebro_client.py`)**:
   - Suporte nativo a `mcp_url` (ou variável `SEGUNDO_CEREBRO_MCP_URL`).
   - `check_health(timeout_sec=3.0)`: Probe rápido de conectividade que retorna `available=False` em prazo <= 3.0s caso o endpoint esteja inacessível.
   - `search`: Detecta endpoint offline previamente e encerra imediatamente retornando `KnowledgeQueryResult(status="INSUFFICIENT_EVIDENCE")` sem travar a execução.
   - `_execute_mcp_tool`: Chamadas HTTP com timeout curto de 5,0s e tratamento de erro estruturado.

3. **Garantia de Segurança e Proteção de Segredos**:
   - Todos os logs, exceções e relatórios passam por `redact_secrets`, garantindo que cabeçalhos `Authorization`, tokens `Bearer` e parâmetros de URL nunca sejam vazados em logs ou no git.

4. **Preservação e Retomabilidade da Worktree (`run_ticket.py`)**:
   - Adicionado diagnóstico no preflight do ciclo de execução informando degradação de MCP sem interromper o fluxo nem descartar o workspace do ticket.

---

## 3. Evidências de Teste e Validação

- **Nova Suíte de Testes**:
  - `tests/test_mcp_fail_fast_usr125.py` (11 testes, todos aprovados em 6.04s):
    - `test_check_optional_mcp_servers_no_config`: PASS
    - `test_check_optional_mcp_servers_empty_mcp_servers`: PASS
    - `test_check_optional_mcp_servers_healthy_server`: PASS
    - `test_check_optional_mcp_servers_offline_fails_fast`: PASS (< 1.5s)
    - `test_check_optional_mcp_servers_redacts_credentials`: PASS (tokens mascarados)
    - `test_env_var_override_darkfac_strict_mcp_config`: PASS
    - `test_build_claude_argv_strict_mcp_flag`: PASS
    - `test_run_claude_injects_strict_mcp_when_degraded`: PASS
    - `test_segundo_cerebro_client_http_health_check_fails_fast`: PASS
    - `test_segundo_cerebro_client_search_fails_fast_with_insufficient_evidence`: PASS
    - `test_segundo_cerebro_client_redacts_auth_token_in_http_failure`: PASS
- **Testes de Regressão**:
  - `tests/test_segundo_cerebro_connector.py` + `tests/line/test_agent_cli.py`: 28 testes aprovados em 17.97s.
- **Portão Oficial Determinístico (`runner.py --quick --local`)**:
  - `syntax_and_types`: PASS
  - `unit_and_integration_tests_parallel`: PASS (2998 passed, 7 skipped, 717.6s)
  - `unit_and_integration_tests_serial`: PASS (57 passed, 1 skipped, 216.9s)
  - Total: 3.063 testes, 3.055 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`).
- **GitHub Actions CI (PR #153)**:
  - `pr-validation (ubuntu-latest)`: PASS (2m08s)
  - `pr-validation (windows-latest)`: PASS (10m51s)
  - `trusted-pr-policy`: PASS (6s)
- **Deploy Dokploy VPS**:
  - Serviço `darkfac-cloud`: status `done` no commit `876e334`
  - Backup & restore drill: `snp3t_darkfac_20261005_141448_4a3d94` (`drill_verified=True`)
