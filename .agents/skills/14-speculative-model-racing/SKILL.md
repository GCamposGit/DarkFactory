---
name: speculative-model-racing
description: Executa corridas especulativas A/B/n em cascata e torneios empíricos com os Top 3 modelos de cada nível de capacidade em tarefas reais da fábrica. Enforça portões de teste determinísticos, mede Pass@1 empírico no Windows, calcula Elo Bradley-Terry e fecha o loop de feedback entre benchmarks sintéticos e realidade produtiva.
---

# 14 - Speculative Model Racing & Empirical Tournament Engine

O **Speculative Model Racing Engine** implementa a seleção dinâmica custo-efetiva e a avaliação empírica contínua de LLMs para a Dark Factory, combinando cascatas especulativas com validação estrita no ambiente de produção.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Especificação da Tarefa**: Requisito, caminhos de arquivo e critérios de aceitação.
- **Top 3 Modelos por Tier de Pareto**:
  1. *Fast Drafter*: Baixo custo e altíssima velocidade (ex.: modelo local $0 ou light cloud).
  2. *Balanced Challenger*: Near-Pareto com alta proximidade de fronteira (FPI >= 95%).
  3. *Frontier Arbiter*: Líder absoluto de capacidade para a classe da tarefa.
- **Comandos de Teste**: Suíte determinística para verificação da solução.

### Ações e Procedimento Executável
1. **Execução em Cascata Especulativa**:
   - O *Fast Drafter* gera a primeira tentativa de código em baixa latência.
   - O código gerado é submetido imediatamente ao **Code Judge Determinístico**:
     - Compilação sintática AST (`py_compile` / linting estrito).
     - Execução da suíte de testes unitários relevante.
   - **Vitória do Drafter**: Se aprovado com 100% de sucesso determinístico, a tarefa é concluída com custo e latência mínimos.
   - **Escalação Transparente**: Se houver falha, a tarefa escala para o *Challenger* ou *Arbiter* de fronteira, garantindo a entrega sem falhas.
2. **Torneio Empírico de Desempenho (Shadow Tournament)**:
   - Avalia modelos concorrentes em micro-tarefas reais (AST, edge cases de UTF-8, mocks para pytest).
   - Calcula taxa de Pass@1 empírico e calibra o Elo Rating (Bradley-Terry).
3. **Análise de Fronteira via CLI**:
   ```powershell
   python -m core.benchmarks.cli status
   python -m core.benchmarks.cli frontier
   ```

### Outputs Estruturados
- **Código Validado**: Solução que passou integralmente no portão determinístico.
- **Empirical Benchmark Ledger**: `.factory/benchmarks/empirical_ledger.json` contendo Pass@1 real, Elo Rating atualizado e telemetria de economia financeira.

### Portões, Política e Validação
- **Portão Determinístico Inviolável**: Nenhuma solução gerada por modelo especulativo ou econômico pode ser aceita sem passar em 100% dos testes e checagens estáticas.
- **Seleção Dinâmica de Modelos**: Proibida a fixação de nomes legados estáticos como defaults; a composição da tríade (Drafter, Challenger, Arbiter) deve ser resolvida dinamicamente a partir do catálogo diário da Fronteira de Pareto.
- **Isolamento de Efeitos**: Execuções especulativas concorrentes operam em worktrees ou sandboxes limpos, sem colisão de estado.

---

## 2. Continuous Self-Improvement & Calibração de Elo

- **Fechamento do Loop Empírico**: Se um modelo com alto índice em benchmarks sintéticos falhar repetidamente no ambiente Windows local da fábrica, seu Elo empírico é rebaixado no ledger, reduzindo sua prioridade nas cascatas especulativas.
