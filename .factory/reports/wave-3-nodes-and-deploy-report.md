# Relatório de Conclusão: Onda 3 — Nós e Deploy (USR-71, USR-81, USR-72, USR-82)

- **Onda**: 3 — Nós e Deploy (`docs/STABILITY_PLAN_2026-09-30.md`, Frente H)
- **Tickets Concluídos**: `USR-71`, `USR-81`, `USR-72`, `USR-82`
- **Data**: 2026-10-03
- **Ambiente de Validação**: Notebook local (`--local`)

---

## 1. Resumo Executivo das Entregas

A Onda 3 estabelece resiliência completa para o sincronismo entre os três nós de execução da Dark Factory (Notebook, Desktop worker e Dokploy VPS) e robustece o pipeline de deploy e backups autônomos pós-merge.

### A. USR-71: Recuperação Autônoma de Processo Legado no Desktop Worker
- **Problema**: Processo legado na porta 8080 sem expor `git_sha` e `restart_safe` causava deadlock no sincronismo.
- **Solução**:
  - `core.infra.node_sync` agora detecta processo legado (`is_legacy = True` quando `git_sha is None` e `restart_safe is not True`).
  - Recuperação autônoma via Windows Scheduled Task (`schtasks /End /TN "DarkFac Test Worker"` seguido de `schtasks /Run /TN "DarkFac Test Worker"`), sem intervenção humana.
  - Sonda aguarda o worker retornar até o timeout configurado.
  - Documentação adicionada em `docs/runbooks/desktop_test_worker.md`.
  - Cobertura de teste com fakes em `tests/test_node_sync.py::test_desktop_legacy_recovers_via_scheduled_task`.

### B. USR-81: Tratamento de Offline Transitório pós-Restart do Worker
- **Problema**: O teste ou sincronismo lia o `/health` imediatamente após `POST /system/restart`; o worker levava alguns segundos para reiniciar e era medido como `offline`, reprovando um deploy que na prática convergiu.
- **Solução**:
  - Loop de espera pós-restart em `core.infra.node_sync.sync` trata estados offline transitórios como `"restarting"` ao longo da janela de timeout.
  - Veredito final só é emitido após a janela expirar; se permanecer offline persistentemente, emite causa explícita com o tempo decorrido.
  - Cobertura de testes com fakes:
    - `tests/test_node_sync.py::test_desktop_restart_transient_offline_treated_as_restarting_and_converges`
    - `tests/test_node_sync.py::test_desktop_restart_persistent_offline_fails_with_clear_cause`.

### C. USR-72: Dokploy Redeploy Robusto, sem Traceback e com Backup Garantido
- **Problema**:
  - Saída em pipe com buffer (590s sem stdout);
  - Exceções não capturadas geravam Traceback cru no terminal;
  - Lógica de teste em produção (`pytest in sys.modules`) decidia sobre a execução do backup;
  - Falhas de convergência pulavam o backup sem registrar o motivo.
- **Solução**:
  - Reconfiguração de line buffering em `sys.stdout` e `sys.stderr` e `flush=True` em todas as emissões de progresso.
  - Tratamento de exceção de topo em `scripts/dokploy_redeploy.py` com saída estruturada JSON (`[STRUCTURED_ERROR]`) e exit codes específicos (`EXIT_USAGE_ERROR = 2`, `EXIT_DEPLOY_FAILED = 1`), eliminando qualquer Traceback cru.
  - Remoção completa de referências a `pytest` e `sys.modules` no código de produção; injeção de `default_backup_runner` e mocks isolados nas fixtures de teste.
  - Registro explícito de motivo em todos os caminhos de skip do backup (`[BACKUP] skipped: --skip-backup flag provided` ou `[BACKUP] skipped: deploy did not converge (all_ok is False)`), além de `[BACKUP] ran:` no sucesso.
  - Integração do escopo adicional do USR-85: verificação `check-main` pós-deploy e emissão estruturada de `[CHECK-MAIN] state=... code=...`.
  - Cobertura de testes em `tests/test_dokploy_redeploy.py`:
    - `test_no_pytest_in_dokploy_redeploy`
    - `test_transport_exception_emits_structured_error_no_traceback`
    - `test_backup_skipped_on_failure_logs_reason`
    - `test_check_main_called_after_node_sync`
    - `test_stage_order_in_redeploy` (ordem garantida: deploy -> node_sync -> check-main -> backup).

### D. USR-82: Idade do Commit e Evidência sem Presunções
- **Problema**: Descrição do USR-78 afirmava semanas sem atualizar sem mensuração real.
- **Solução**:
  - `NodeStatus` em `core.infra.node_sync` agora registra `commit_date` para medir a idade real do commit do nó.
  - Texto do USR-78 no ledger `.factory/demands/demands.json` ajustado para refletir estritamente a evidência observada (`ahead 3, behind 9`).
  - Cobertura em `tests/test_node_sync.py::test_node_status_records_commit_date`.

---

## 2. Validação Focada Local

```powershell
python -m pytest tests/test_node_sync.py tests/test_dokploy_redeploy.py -q
```
- **Total de Testes**: 108 executados (106 passaram, 2 skips intencionais de SO/hardware)
- **Tempo de Execução**: ~7s no notebook
- **Higiene da Árvore**: 100% limpa, sem poluição de arquivos de estado
