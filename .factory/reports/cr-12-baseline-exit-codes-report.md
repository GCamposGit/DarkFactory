# Relatório de Conclusão — CR-12: Alinhar Códigos de Saída do HF-01

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-12 (Trilha HF-01 Confiança, depende diretamente de CR-03)  
**Achado endereçado:** F14 (códigos de saída ambíguos no CLI de baseline e falta de persistência de diagnóstico em falhas de fontes requeridas)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `core/planning/baseline_cli.py`, `core/planning/baseline_verify.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Taxonomia Determinística de Códigos de Saída**:
   - **Exit Code 0 (Sucesso Contratado Exclusivo)**:
     - Retornado **apenas** quando todas as fontes requeridas foram lidas com sucesso durante `collect` e os 4 arquivos atômicos foram gerados (`snapshot.json`, `BASELINE.md`, `sources.json`, `manifest.json`).
     - Em `verify`, retornado exclusivamente quando a verificação semântica do replay e integridade estrutural passam com `[BASELINE_VERIFY_PASS]`.
     - Fontes opcionais (`required: false`) ausentes no disco não impedem o sucesso contratado; são observadas com status `"missing"`, visíveis sem serem promovidas a fontes lidas.
   - **Exit Code 2 (Entradas Requeridas Ausentes / Falta de Fontes)**:
     - Se qualquer fonte com `required: true` estiver ausente ou ilegível (`status != SourceStatus.READ`), o comando `collect` **persiste integralmente o relatório parcial no disco** (`snapshot.json`, `BASELINE.md`, `sources.json`, `manifest.json`), imprime o sumário JSON de diagnóstico no `stdout`, emite `[BASELINE_ERROR] REQUIRED_SOURCE_MISSING: ...` no `stderr` e encerra com código **2**.
     - Em `verify`, se `--root`, `--manifest`, arquivos de fontes requeridas ou arquivos de catálogo/claims estiverem ausentes no disco ou no snapshot com status diferente de `read`, encerra com código **2**.
   - **Exit Code 3 (Corrupção de Dados / Violação Estrutural / Replay Inválido)**:
     - Acionado imediatamente em caso de adulteração de fingerprints, hashes SHA-256 de fontes/catálogo/claims/probes, estados forjados de prontidão (`hf02_readiness`), dimensões alteradas, adulteração de dependências de itens, quebra de integridade referencial de evidências ou ciclos não resolvidos.
     - Emite `[BASELINE_CORRUPTED] <motivo>` no `stderr`.
   - **Comportamento Padrão de Argparse**:
     - Flag `--help` retorna **exit 0** com o texto de uso no stdout.
     - Argumentos desconhecidos ou flags mandatórias omitidas retornam **exit 2** conforme padrão POSIX/Python argparse.

2. **Sanitização e Tipagem Estruturada de Erros**:
   - Mensagens de erro no `stderr` utilizam marcadores padronizados: `[BASELINE_ERROR] <motivo>` para código 2 e `[BASELINE_CORRUPTED] <motivo>` para código 3.
   - Exceções internas como `BaselineCliError` não vazam stack traces desnecessários ou tipos desformatados.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `core/planning/baseline_cli.py` | Importado `SourceStatus`; implementada a persistência atômica dos 4 artefatos de diagnóstico antes da checagem de fontes requeridas faltantes no `_collect`; retorno de código 2 com relatório em disco; sanitização do tratamento de exceções em `_verify` e `main()` separando categorias 2 e 3. |
| `core/planning/baseline_verify.py` | Adicionada checagem em Phase 2 de catálogo para fontes obrigatórias com status diferente de `READ`, emitindo erro `REQUIRED_SOURCE_MISSING` com `exit_code=2`. |
| `tests/test_baseline_cli.py` | Importados `subprocess` e `sys`; adicionados 8 testes de aceitação executando subprocessos reais (`subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", ...])`) para validar exit codes, stdout, stderr e persistência de relatórios parciais. |

---

## 3. Validação Executada

### 3.1 Testes de Linha de Comando (Subprocessos e Unidade)
```powershell
python -m pytest tests/test_baseline_cli.py -v
```
Resultado: **29 passed in 2.84s**

### 3.2 Harness Determinístico Completo
```powershell
python core/harness/runner.py --quick
```
Resultado: **576 passed, 2 skipped in 67.66s**
`[STEP_PASS] syntax_and_types`
`[STEP_PASS] unit_and_integration_tests`
`[HARNESS_PASS]`
