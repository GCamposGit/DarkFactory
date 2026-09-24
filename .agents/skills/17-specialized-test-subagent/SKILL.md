---
name: specialized-test-subagent
description: Delega a execução, monitoramento e sumarização de suítes de testes a subagentes rápidos e baratos (flash_lite, haiku, grok-low, luna-low ou qwen-fast local a $0). Isola logs volumosos em disco (.factory/test_logs/) e sintetiza relatórios destilados (DistilledTestReport) para o contexto principal, evitando poluição de tokens e acelerando loops de iteração em qualquer harness (Antigravity, Codex, Grok, Claude Code).
---

# 17 - Specialized Test Subagent: Execução e Destilação Multi-Harness

O **Specialized Test Subagent** isola a execução de testes em subagentes eficientes e econômicos, blindando a janela de contexto do agente orquestrador principal contra centenas de linhas de logs, stack traces e fixtures verbosas.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Alvo do Teste**: Caminho do arquivo ou diretório de testes (`--target tests/test_foo.py`).
- **Escopo**: Nível de granularidade (`file`, `suite`, `unit`, `integration`).
- **Harness e Modelo Ativo**: Seleção entre modelo local $0 (Ollama) ou modelo "light" do harness na nuvem (`flash_lite`, `haiku`, `grok low`, `luna low`).

### Ações e Procedimento Executável
1. **Seleção Determinística de Executor**:
   - **Local ($0, Ollama `localhost:11434`)**: Obrigatório para micro-iterações de TDD (Red-Green-Refactor) com suítes rápidas. Modelos: `qwen-code-fast:latest` ou `qwen2.5-coder:7b`.
   - **Modelo Light do Harness**: Para marcos de branch, validações em CI/CD ou suítes extensas (>32k tokens).
   - **Prefira alvos focados**: para iteração, aponte `--target` para o
     arquivo/módulo tocado em vez da suíte inteira. Uma execução completa
     (`--scope all`) passa pelo `suite_lock` global (`core/harness/suite_lock.py`,
     via `tests/conftest.py`) e pode esperar se outro harness/agente no
     mesmo host já estiver validando — isso é intencional (evita disputa de
     CPU/sqlite), mas não é o caminho rápido de TDD.
2. **Invocação Isolada por Harness**:
   - **Antigravity**: `invoke_subagent(TypeName="self", Role="Specialized Test Runner", Model="flash_lite", Prompt=...)`.
   - **OpenAI / Codex**: Modelo com `reasoning_effort: "low"` para testes rotineiros; escala para `max` apenas em falhas assíncronas complexas.
   - **Grok Build**: `grok build --reasoning-effort low --exec "python ... "`.
   - **Claude Code**: Subagente declarativo com `claude-3-5-haiku` sem *extended thinking*.
3. **Persistência de Logs em Disco**:
   - O log integral é gravado em `.factory/test_logs/<run_id>.log`.
   - O subagente sintetiza um relatório condensado (`DistilledTestReport`).

### Outputs Estruturados
- **DistilledTestReport**: Estrutura contendo:
  - `status`: `PASSED` ou `FAILED`.
  - `total_tests`, `passed`, `failed`, `skipped`.
  - `failures_summary`: Trecho exato da falha (arquivo, linha, mensagem de erro) sem stack trace desnecessário.
  - `log_file_path`: Caminho absoluto do log em disco.
- **Insumos de Evidência**: Dados formatados prontos para subsidiar o `EnvironmentEvidence` no `validation-harness`.

### Portões, Política e Validação
- **Fidelidade de Resultados**: O relatório destilado não pode ocultar falhas nem inventar contagens de testes.
- **Integração com ReadinessGate**: Os resultados alimentam a validação determinística sem flexibilizar critérios de aprovação.
- **Proteção de Contexto**: Proibido despejar logs brutos no chat principal quando o runner puder emitir o resumo destilado.

---

## 2. Continuous Self-Improvement & RCA de Testes

- **RCA em Flaky Tests**: Se um teste falhar intermitentemente entre rodadas, isole a fixture e registre o padrão no ledger via `python -m core.learning.cli rca`.
