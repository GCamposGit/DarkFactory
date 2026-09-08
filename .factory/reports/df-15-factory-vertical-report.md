# DF-15 — Vertical de Bugfix Autônomo da Fábrica

## Resultado

O ticket **DF-15** foi implementado com sucesso, conectando o caminho crítico completo da fábrica autônoma:
**Issue com critérios de aceitação $\rightarrow$ Proposta de Patch com contenção Sandbox $\rightarrow$ Verificação do Candidato $\rightarrow$ Holdout Independente Protegido $\rightarrow$ Checkpoint Durável e Retomada Pós-Crash $\rightarrow$ Emissão de Evidências**.

Todas as fragilidades e erros observados na tentativa anterior foram corrigidos:
1. **Blindagem de Invocação de Terminal**: Injeção da raiz do repositório em `sys.path` no topo de `core/orchestrator/cli.py`, permitindo execução direta sem `ModuleNotFoundError`.
2. **Bootstrap de Fixture na CLI**: Suporte a `--init-fixture` e ao subcomando `init-fixture` para inicializar a suíte de demonstração em qualquer `workdir`.
3. **Resolução de Holdout Supervisor-Level**: Fallback transparente para o holdout protegido em `.factory/holdout/df15_factory_vertical.py` quando executado em diretórios isolados.
4. **Validação Rígida do Candidato**: Falha rápida com diagnóstico acionável (`FactoryVerticalError`) se o comando de verificação do candidato falhar antes de invocar o holdout.
5. **Idempotência e Retomada Pós-Crash**: Garantia de que crashes após a aplicação de patch não duplicam débitos de orçamento nem efeitos colaterais na retomada.

---

## Entregas Principais

1. **CLI e Orquestrador Vertical (`core/orchestrator/cli.py`)**:
   - `FactoryVertical`: Orquestrador com 4 passos encadeados e recuperáveis:
     - Passo 0: `_issue_step` (digestão da issue e objetivo com hash SHA-256).
     - Passo 1: `_patch_step` (reserva de orçamento, geração de patch, validação de sandbox de caminhos, aplicação atômica, registro de tentativa).
     - Passo 2: `_candidate_step` (execução do teste do candidato via `ProcessSandbox` com fail-fast).
     - Passo 3: `_holdout_step` (verificação independente cega fora do escopo gravável do candidato).
   - `FactoryVerticalConfig`: Contrato Pydantic v2 estrito e imutável para configuração de tarefas.
   - `FactoryRunEvidence` & `FactoryRunResult`: Registro estruturado com todos os hashes criptográficos, contagem de testes, métricas de telemetria e custo medido em USD.
   - Ponto de entrada CLI completo (`run`, `init-fixture`, `--config`, `--workdir`, `--provider`, `--resume`, `--init-fixture`).

2. **Holdout Verifier Independente (`.factory/holdout/df15_factory_vertical.py`)**:
   - Módulo independente de aceitação contendo casos de teste protegidos (`0+0=0`, `-7+4=-3`, `13+-5=8`) que barram patches "trapaceiros" (overfitting).

3. **Fixture do Defeito (`tests/fixture_factory_bug.py`)**:
   - Gerador de ambiente com o defeito de subtração na operação `add`, teste de aceitação básico visível e cópia do holdout protegido.

4. **Configuração Padrão (`factory_vertical.config.json`)**:
   - Especificação fechada do ticket `DF15-CALCULATOR-ADD` com teto orçamentário de \$0.05, timeout e allowed paths.

5. **Suíte Focal Abrangente (`tests/test_factory_vertical.py`)**:
   - `test_issue_generates_real_patch_and_independent_holdout`: Ciclo de ponta a ponta com evidências válidas.
   - `test_crash_after_patch_resumes_without_duplicate_effect_or_charge`: Crash forçado após patch e recuperação sem recarga nem duplicação.
   - `test_provider_cannot_patch_outside_allow_list`: Tentativa de alterar arquivo fora de `allowed_paths` barrada pela sandbox.
   - `test_candidate_check_failure_blocks_holdout`: Patch inválido ou com erro de sintaxe/lógica falha no teste do candidato e aborta.
   - `test_holdout_rejection_blocks_delivery`: Patch que satisfaz apenas o teste visível é rejeitado pelos casos cegos do holdout.
   - `test_cli_main_execution_and_fixture_init`: Invocação programática de `main(["init-fixture"])` e `main(["run"])`.
   - `test_cli_direct_subprocess_invocation`: Invocação via subprocesso real no Windows provando ausência de `ModuleNotFoundError`.

---

## Validação Executada

1. **Suíte Focal**:
   ```powershell
   python -m pytest tests/test_factory_vertical.py -v
   ```
   **Resultado**: 7 passed em 1.78s.

2. **Invocação Direta via CLI**:
   ```powershell
   python C:\dev\DarkFac\core\orchestrator\cli.py run --workdir C:\dev\DarkFac\.factory\demo_workdir --init-fixture
   ```
   **Resultado**: `[FACTORY_PASS] df15_factory_vertical` emitido com `status: SUCCEEDED`.
