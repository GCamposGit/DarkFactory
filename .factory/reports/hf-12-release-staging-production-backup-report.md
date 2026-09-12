# Relatório de Conclusão: HF-12 — Build, Staging, Aceite de Cliente Pagante, Produção, Smoke, Rollback e Backups Exercitados

- **Ticket**: `HF-12`
- **Data**: 11/09/2026
- **Status**: `COMPLETED`
- **Governança**: `HYBRID_WORKFLOW_PLAN_2026-09-08` (Seções 5, 7, 8, 9, 10, 11, 12, linhas 264, 297, 298) e `HYBRID_AUTONOMY_REQUIREMENTS` (Seções 5, 7, Cenário G8)
- **Reúso / Complemento**: `INFRA-08`, `INFRA-09`, `DF-20`, `HF-11`, Skills 04, 05 e 07
- **Módulos Afetados**: `core.orchestrator.release_pipeline`, `core.infra.backup_service`, `core.orchestrator.release_cli`, `tests/test_hf12_release_staging_production_backup.py`, `docs/handoffs/HF-12.md`

---

## 1. Contexto e Objetivos

O ticket **HF-12** implementa a esteira segura de entrega contínua, promoção de artefatos e resiliência de dados para a Dark Factory, garantindo operação headless robusta tanto para projetos internos experimentais quanto para sistemas comerciais com faturamento ativo.

Anteriormente, o processo de publicação carecia de uma separação estrita entre aprovação técnica de CI e aceite de negócio/cliente, gerando risco de recompilações discrepantes entre ambientes ou promoções indevidas em produção. Além disso, a gestão de recuperação exigia validação determinística de restauração de backups (restore drill) com conferência de integridade criptográfica.

O HF-12 consolida as seguintes garantias:
1. **Promoção do mesmo artefato**: O mesmo digest SHA-256 gerado e testado em staging é promovido para produção (zero recompilações).
2. **Portão de aceite para cliente pagante (Cenário G8)**: Projetos `COMMERCIAL_PAID` são estritamente bloqueados em produção até o registro de recibo de aceite (`ClientAcceptanceReceipt`).
3. **Preflight e Smoke tests pós-deploy**: Validação ativa de destinos antes do deploy e testes de jornada crítica pós-deploy com limites rígidos de latência.
4. **Rollback automático e recuperação isolada (Cenário G8)**: Reversão imediata para a última versão estável em falha de release, abrindo incidente isolado sem paralisar outros projetos.
5. **Backups consistentes e restore drill em sandbox (INFRA-08 & R2)**: Empacotamento com digest SHA-256 e validação determinística de 100% dos checksums restaurados em sandbox isolado.
6. **Interface CLI headless padronizada**: Suporte completo a `--json`, status codes semânticos e saída UTF-8 blindada no Windows.

---

## 2. Entregas e Invariantes Comprovadas

### 2.1. Mesmo Artefato Promovido (Zero Recompilações)
- `BuildArtifact` calcula um `artifact_digest` determinístico via SHA-256 sobre a composição dos arquivos e manifestos.
- `deploy_staging` e `deploy_production` utilizam o mesmo digest imutável.
- Tentativas de deploy de artefatos inexistentes falham com `ArtifactNotFoundError`.

### 2.2. Portão de Aceite de Cliente Pagante (Cenário G8)
- Distinção clara em `ProjectTier`: `INTERNAL_EXPERIMENTAL` vs `COMMERCIAL_PAID`.
- Para projetos `COMMERCIAL_PAID`, a promoção para produção rejeita terminantemente sem recibo assinado (`ClientAcceptanceRequiredError` / exit code 2 no CLI).
- Registro do recibo através de `record_client_acceptance` autoriza a liberação da release com registro do autorizante e timestamp.

### 2.3. Preflight Checks e Smoke Tests Pós-Deploy
- `DestinationPreflight` valida a prontidão operacional do endpoint de destino (staging/produção).
- `JourneySmokeTest` executa checagens ativas das rotas críticas (ex: `/healthz`), exigindo status HTTP 200 e tempo de resposta < 2000ms.

### 2.4. Rollback Automático e Recuperação Isolada (Cenário G8)
- Em falha de release ou smoke test em produção, o pipeline aciona automaticamente o rollback para o `stable_production_artifact` anterior.
- Emite `RollbackReceipt` estruturado detalhando a versão restaurada e a falha ocorrida.
- Transiciona o status do projeto para `RECOVERY_REQUIRED`, abrindo canal de remediação isolado sem afetar filas e pipelines de projetos concorrentes na fábrica autônoma.

### 2.5. Serviço de Backup Consistente e Restore Drill em Sandbox (INFRA-08 & R2)
- `CloudBackupService` cria snapshots consistentes (`tar.gz`) com digest SHA-256 e contabilidade de bytes.
- Execução de `restore_drill`: descompactação em diretório sandbox isolado temporário, recalculando o checksum de cada arquivo restaurado e comparando com o snapshot original.
- Emite `RestoreDrillResult` comprovando restauração de 100% dos arquivos com integridade verificada.
- Política de retenção configurável (`purge_expired`) para descarte seguro de snapshots expirados.

### 2.6. CLI Headless Operacional
- Subcomandos `build`, `deploy-staging`, `accept`, `deploy-production` e `backup-and-drill` em `core/orchestrator/release_cli.py`.
- Formatação estruturada com `--json` e códigos de retorno estritos (0 = sucesso, 1 = falha genérica/smoke, 2 = bloqueio de aceite, 3 = falha de restore drill).

---

## 3. Matriz de Testes e Evidências

| Teste | Escopo / Invariante | Resultado |
|---|---|---|
| `test_build_artifact_generates_consistent_digest` | Geração consistente de artefato com digest SHA-256 reprodutível | PASSED |
| `test_deploy_staging_succeeds_with_preflight_and_smoke` | Deploy em staging com preflight verde e smoke test aprovado | PASSED |
| `test_promote_same_artifact_to_production` | Promoção do mesmo artifact_digest de staging para produção (zero recompilações) | PASSED |
| `test_commercial_paid_requires_acceptance_receipt_scenario_g8` | Cenário G8: bloqueio de produção para cliente pagante sem recibo assinado | PASSED |
| `test_record_client_acceptance_allows_production_deploy` | Registro de aceite autoriza promoção para produção | PASSED |
| `test_production_smoke_failure_triggers_automatic_rollback_scenario_g8` | Cenário G8: falha no smoke de produção dispara rollback automático imediato | PASSED |
| `test_production_failure_isolates_recovery_without_stopping_others_scenario_g8` | Cenário G8: falha isola projeto em RECOVERY_REQUIRED sem parar projetos paralelos | PASSED |
| `test_create_backup_snapshot` | Criação de snapshot consistente com checksum SHA-256 e metadados | PASSED |
| `test_restore_drill_in_isolated_sandbox_verifies_all_checksums` | INFRA-08: restauração em sandbox com 100% dos checksums conferidos | PASSED |
| `test_backup_retention_purge` | Política de retenção expira backups antigos preservando os válidos | PASSED |
| `test_cli_headless_release_commands` | Execução headless via CLI com `--json` e saída UTF-8 no Windows | PASSED |

---

## 4. Validação do Portão Oficial

- **Comando**: `python core/harness/runner.py --quick`
- **Total de Testes**: **792 descobertos** (790 aprovados, 2 skipped, 0 falhas)
- **Status do Portão**: **`[HARNESS_PASS]`**
- **Exit Code**: `0`

---

## 5. Próximo Sucessor

O próximo ticket na sequência do Plano Híbrido é **HF-14** (Telegram do owner e n8n Community simples, após verificar instalação existente).
