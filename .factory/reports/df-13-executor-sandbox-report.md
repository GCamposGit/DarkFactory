# DF-13 — Executor Sandbox e Ciclo de Processos

## Resultado

O ticket **DF-13** foi implementado com sucesso em worktree dedicada (`codex/df-13-executor-sandbox`), entregando a interface de alto nível do supervisor para execução de agentes, isolamento de processos com timeout e encerramento em árvore de filhos, contenção estrita de caminhos de arquivos e rede, e integração direta com o subsistema de orçamento `ExecutionBudgetManager`.

## Entregas

1. **Abstração de Provedores (`core/execution/providers.py`)**:
   - Protocolo `ModelProvider` e payload imutável `ProviderResponse`.
   - Separação rígida entre inferência pura e execução de ferramentas.
   - `MockModelProvider` determinístico suportando custos medidos e políticas de custos desconhecidos (`REJECT`, `ESTIMATE`, `CONSERVATIVE_MAX`).

2. **Contenção e Sandbox (`core/execution/sandbox.py`)**:
   - `PathContainment`: Validação de caminhos permitidos (`allowed_paths`), bloqueio de travessia de diretório (`..`) e proteção inegociável de arquivos de governança (`MISSION.md`, `FACTORY_RULES.md`, `FACTORY_GOVERNANCE.md`).
   - `NetworkContainment`: Restrição de tráfego de rede para destinos confiáveis e controle estrito de loopback.
   - `ProcessSandbox` & `kill_process_tree`: Execução de subprocessos com ambiente reduzido (UTF-8 forçado), limite de timeout e garantia de encerramento em árvore (filhos e descendentes) tanto no Windows (`taskkill /F /T /PID`) quanto no POSIX.

3. **Ciclo de Vida do Agente (`core/execution/agent_executor.py`)**:
   - Contratos tipados `TaskSpec`, `ExecutionRun`, `Checkpoint`.
   - Operações determinísticas `start`, `resume` a partir de checkpoint, `cancel` com liberação imediata de reservas de orçamento.
   - Registro de tentativas auditáveis (`AttemptRecord`) com hashes SHA-256 de entrada/saída, medição de latência e consumo de tokens.

4. **Suíte Focal de Testes (`tests/test_executor_contract.py`)**:
   - `test_path_containment_allowed_and_escapes`
   - `test_network_containment_rules`
   - `test_process_sandbox_timeout_and_tree_kill`
   - `test_executor_start_cancel_and_budget`
   - `test_executor_step_execution_and_resume_checkpoint`
   - `test_provider_inference_and_unknown_cost_policy`

## Validação Executada

- `python -m pytest tests/test_executor_contract.py -v` — **6 passed** (0.80s).
- Suíte geral de regressão em execução na worktree.
