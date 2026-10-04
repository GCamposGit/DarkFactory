# Relatório de Conclusão: USR-115

**Ticket**: USR-115  
**Título**: Grill não reenvia pergunta ao owner quando o envio ao Telegram falha  
**Status**: `completed`  
**Data**: 2026-10-04  
**PR**: [#151](https://github.com/GCamposGit/DarkFactory/pull/151) (commit `763c60a`)  
**Deploy Dokploy VPS**: Convergido (`763c60a`), backup drill verificado (`snp3t_darkfac_20261004_211457_606c0a`)  

---

## 1. Contexto e Problema

No incidente de 01/10 (run `run-83bd3712924d`), a execução pausou em `waiting_human` no estágio `grill` porque a notificação via Telegram falhou (ex.: erro 401 ou indisponibilidade transitória). O arquivo `GRILL_PENDING.json` ficou gravado no workspace com `notified=False`, mas nenhum processo ou ciclo em background drenava ou tentava retransmitir a pergunta não notificada ao Owner. Como consequência, o Owner não recebia a notificação e o run ficava paralisado até intervenção manual.

---

## 2. Solução Implementada

1. **Alerta Imediato e Estruturado em Falha de Envio (`stage_grill.py`)**:
   - Criada a rotina `_alert_owner_bot` em `core/line/stage_grill.py`. Se `notify_owner_via_bot` falhar com qualquer exceção ou erro de API, o erro é registrado no log em nível `ERROR` e um alerta de emergência estruturado é enviado para o bot do owner (`BOT_ROLE_OWNER`), prevenindo silêncio operacional.
   - Proteção hermética contra chamadas vivas de rede em ambiente de testes automatizados (`PYTEST_CURRENT_TEST`).

2. **Reenvio Automático de Perguntas Pendentes (`stage_grill.py`)**:
   - Implementada a função `resend_unnotified_grill(workspace_path, notifier)`.
   - Lê `GRILL_PENDING.json`, valida se `notified == False`, tenta novo disparo via Telegram e atualiza o arquivo com `notified=True` e `notified_at` ao ter sucesso.

3. **Varredor Periódico do Worker (`cloud_worker.py`)**:
   - Integrado `grill_resender` na função `sweep_waiting_human_requests` em `core/orchestrator/cloud_worker.py`.
   - Sempre que o sweeper encontra um run aguardando resposta humana em estágio `grill`, ele executa `resend_unnotified_grill` para reenviar perguntas pendentes caso a notificação original tenha falhado.

4. **Higiene e Desempenho de Testes**:
   - Otimizado `tests/_tree_hygiene.py` para ignorar pastas transitórias volumosas (`.factory/backups/`, `.factory/test_logs/`, `tmp/`), acelerando o escaneamento de 27s para 0.05s por teste.
   - Marcado `tests/test_git_autonomy_ci_gate.py` como serial para equilíbrio de latência no gate.
   - Adicionada instrumentação por worker xdist (`tests/conftest.py`).

---

## 3. Evidências de Teste e Validação

- **Testes Unitários e de Integração**:
  - `tests/line/test_stage_grill.py`:
    - `test_failed_notification_logs_error_and_alerts_owner` (aprovado)
    - `test_resend_unnotified_grill` (aprovado)
  - `tests/test_cloud_worker_active_loop.py`:
    - `test_human_probe_sweeper_resends_unnotified_grill` (aprovado)
- **Portão Oficial Determinístico (`runner.py --quick --local`)**:
  - `syntax_and_types`: PASS (0.2s)
  - `unit_and_integration_tests_parallel`: PASS (2987 passados, 7 pulados, 815s)
  - `unit_and_integration_tests_serial`: PASS (57 passados, 1 pulado, 225s)
  - Total: 3.052 testes, 3.044 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`).
- **GitHub Actions CI (PR #151)**:
  - `pr-validation (ubuntu-latest)`: PASS
  - `pr-validation (windows-latest)`: PASS
  - `trusted-pr-policy`: PASS
- **Deploy Dokploy VPS**:
  - Serviço `darkfac-cloud`: status `done`, commit `763c60a`
  - Backup & restore drill: `snp3t_darkfac_20261004_211457_606c0a` (`drill_verified=True`)
