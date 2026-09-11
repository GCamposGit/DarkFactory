# Relatório de Conclusão — CR-03: Integridade Estrutural e Semântica no Verify do Baseline

O ticket **CR-03** (Trilha HF-01 Confiança da Baseline) foi concluído com sucesso, remediando em definitivo o achado impeditivo **F04** e os requisitos de integridade estrutural, manifesto vinculado e replay semântico formalizados em docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md e docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md.

---

## 1. Escopo e Invariantes Atendidas

1. **Validação Estrutural e Relacional (Fase 1)**:
   - Unicidade estrita de IDs: duplicatas em source_observations, items ou claims causam rejeição com exit 3 (SNAPSHOT_DUPLICATE_*).
   - Referências de evidências: todo ID em item.evidence_ids deve pertencer às claims do snapshot ou às probe_observations. Referências a IDs desconhecidos falham com exit 3 (EVIDENCE_REFERENCE_INVALID).
   - Referências de issues: todo ID em item.issue_ids deve pertencer às issues do snapshot. Discrepâncias falham com exit 3 (ISSUE_REFERENCE_INVALID).
   - Integridade causal e detecção de ciclos: se houver ciclo no grafo de dependências, dependency_cycle deve estar explicitamente presente em locker_codes e em issues. Caso contrário, rejeição com exit 3 (DEPENDENCY_CYCLE_UNRESOLVED).
   - Integridade de hash de fontes: o source_fingerprint do snapshot deve corresponder estritamente ao cálculo sobre as observações persistidas (SNAPSHOT_FINGERPRINT_MISMATCH, exit 3).

2. **Validação de Manifesto e Fontes no Disco (Fase 2)**:
   - Presença obrigatória de --root e --manifest selecionado pelo operador/supervisor para verificação completa com replay. Sem eles, o verify emite diagnóstico estrutural mas falha fechado com exit 2 (REPLAY_DATA_REQUIRED), nunca emitindo [BASELINE_VERIFY_PASS].
   - Correspondência do manifesto com o snapshot: snapshot_id, ase_sha, source_fingerprint e source_relative_paths devem conferir exatamente. Divergências causam exit 3 (MANIFEST_*_MISMATCH).
   - Hashes no disco: validação de SHA-256 para catalog_sha256, claims_sha256 e probe_config_sha256.
   - Fontes no disco: para toda fonte com status READ, o arquivo deve existir e bater o SHA-256. Fonte alterada causa exit 3 (SOURCE_HASH_CHANGED). Fonte obrigatória ausente encerra imediatamente com exit 2 (SOURCE_NOT_REPLAYABLE), prevenindo que a ausência de insumos seja falsamente interpretada como corrupção (exit 3) na fase de replay.

3. **Replay Semântico Determinístico Offline (Fase 3)**:
   - Recomputa a reconciliação localmente via econcile_baseline utilizando os inputs vinculados, **sem chamadas de rede**, reaproveitando probes persistidos e sanitizados.
   - Detecta claims forjadas diretamente no JSON do snapshot ao comparar com as claims do disco carregadas via manifesto (CLAIMS_MISMATCH, exit 3).
   - Compara todos os campos derivados entre snapshot e recomputação:
     - source_fingerprint
     - completeness
     - hf02_readiness
     - locker_codes
     - Itens: conjunto de IDs, implementation, integration, operation, declared_status, dependencies, vidence_ids e issue_ids.
     - Issues: conjunto de IDs e detalhes estruturais (code, severity, 	arget_package, item_ids, source_ids).
   - Mutações isoladas em qualquer uma dessas dimensões falham com exit 3 ([BASELINE_CORRUPTED]).

---

## 2. Arquivos Modificados / Criados

- [core/planning/baseline_verify.py](c:/dev/DarkFac/core/planning/baseline_verify.py):
  - Implementação completa do verificador erify_snapshot(snapshot, *, root=None, manifest=None) -> VerificationReport cobrindo as 3 fases de integridade estrutural, manifesto e replay semântico determinístico.
- [core/planning/baseline_models.py](c:/dev/DarkFac/core/planning/baseline_models.py):
  - Remoção de mode="before" nos validadores de elative_path em BaselineSourceSpec e SourceObservation, corrigindo a interoperabilidade com model_validate_json.
  - Contratos BaselineManifest e VerificationReport.
- [core/planning/baseline_cli.py](c:/dev/DarkFac/core/planning/baseline_cli.py):
  - Integração do comando erify com --manifest, suporte à classe BaselineCorruptionError (exit code 3) e BaselineCliError (exit code 2).
- [core/planning/__init__.py](c:/dev/DarkFac/core/planning/__init__.py):
  - Exportação de BaselineManifest, VerificationReport e erify_snapshot.
- [	ests/test_baseline_cli.py](c:/dev/DarkFac/tests/test_baseline_cli.py):
  - Fixture atualizada com declaração estruturada de item via markdown_table.
  - Bateria completa de testes de mutações isoladas: readiness, dimensões, blockers, dependências, claims forjadas, manifesto adulterado, fontes alteradas e faltantes, execução direta da API Python e teste oficial de reprodução do achado F04.

---

## 3. Evidências de Validação

### Reprodução do Achado F04
No achado original, a adulteração do snapshot emitia [BASELINE_VERIFY_PASS] com exit 0. Agora:
- 	est_reproduce_f04_finding_now_fails_with_corruption_exit_3: **PASSED** (retorna exit 3, BASELINE_CORRUPTED).
- eproduce_baseline_runtime.py: detecta e bloqueia todas as 11 adulterações do cenário F04 com exit 3.

### Suíte Focal do Módulo
Comando: python -m pytest tests/test_baseline_catalog.py tests/test_baseline_sources.py tests/test_baseline_probes.py tests/test_baseline_cli.py tests/test_baseline_reconcile.py -v
Resultado:
`
55 passed, 1 skipped in 0.42s (100% PASS)
`

### Quick Harness Oficial da Fábrica
Comando: python core/harness/runner.py --quick
Resultado:
`
[STEP_PASS] syntax_and_types
[STEP_PASS] unit_and_integration_tests
[TEST_COUNT] count=532
[HARNESS_PASS] 530 passed, 2 skipped in 94.00s
`
