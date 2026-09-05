---
name: speculative-model-racing
description: Executa corridas especulativas A/B/n em cascata e torneios empíricos com os Top 3 modelos de cada nível de capacidade em tarefas reais da fábrica. Enforça portões de teste determinísticos, mede Pass@1 empírico no Windows, calcula Elo Bradley-Terry e fecha o loop de feedback entre benchmarks sintéticos e realidade produtiva.
---

# 14 - Speculative Model Racing & Empirical Tournament Engine

O **Speculative Model Racing Engine** implementa o estado da arte em seleção dinâmica custo-efetiva e avaliação empírica contínua de LLMs para a Dark Factory.
Inspirado nos avanços de **RouteLLM** (LMSYS / UC Berkeley 2024), **FrugalGPT** (Stanford 2023), **Speculative Cascades** (Google Research 2024) e **Multi-Objective Epsilon-Dominance**:
- O sistema vai além de números estáticos de SWE-bench sintético, aferindo a capacidade real dos modelos no ambiente de produção do usuário (Windows PowerShell, AST checks, pytest fixtures e determinismo estrito).
- Executa cascatas especulativas com os **Top 3 modelos** por nível de capacidade, aceitando a solução do modelo rápido/econômico apenas quando aprovada deterministicamente no harness de validação.

---

## 🏛️ Arquitetura da Corrida Especulativa

```text
               ┌────────────────────────────────────────────────────────┐
               │                TAREFA DA DARK FACTORY                  │
               │   (Complexidade: Critical | High | Medium | Local)     │
               └───────────────────────────┬────────────────────────────┘
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │         TOP 3 SELEÇÃO POR TIER & PROXIMIDADE           │
               │  1. Fast Drafter       (Baixo custo / Alta velocidade) │
               │  2. Balanced Challenger (Near-Pareto FPI >= 95%)       │
               │  3. Frontier Arbiter   (Líder absoluto de capacidade)  │
               └───────────────────────────┬────────────────────────────┘
                                           │
                        [Etapa 1: Geração Especulativa]
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │           FAST DRAFTER (ou Challenger Local $0)        │
               │          Gera a primeira tentativa em < 2s             │
               └───────────────────────────┬────────────────────────────┘
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │       PORTÃO DE VALIDAÇÃO DETERMINÍSTICO (Code Judge)  │
               │  - py_compile & Type hints (AGENTS.md)                 │
               │  - AST Check & Linter determinístico                   │
               │  - Execução de Testes Unitários de Regressão           │
               └───────────────────────────┬────────────────────────────┘
                                           │
                       ┌───────────────────┴───────────────────┐
                       │                                       │
                  [Passou 100%]                           [Falhou / Bug]
                       │                                       │
                       ▼                                       ▼
       ┌───────────────────────────────┐       ┌───────────────────────────────┐
       │     VITÓRIA DO DRAFTER        │       │    ESCALONAMENTO INVISÍVEL    │
       │ - Custo marginal: ~$0.005     │       │ - Arbiter Frontier (ex: Astra)│
       │ - Latência: ultra-baixa       │       │ - Regenera e valida solução   │
       │ - Economia de até 98%         │       │ - Garante entrega sem falhas  │
       └───────────────┬───────────────┘       └───────────────┬───────────────┘
                       │                                       │
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │              EMPIRICAL BENCHMARK LEDGER                │
               │  - Atualiza Pass@1 real na Dark Factory                │
               │  - Atualiza Elo Rating (Bradley-Terry)                 │
               │  - Registra economia acumulada ($ saved)               │
               │  - Persiste em .factory/benchmarks/empirical_ledger.json│
               └────────────────────────────────────────────────────────┘
```

---

## 🎯 Os 3 Modos Operacionais

### 1. Live Speculative Racing (Produção em Tempo Real)
- Ao receber uma tarefa na fábrica, em vez de enviar cegamente para o modelo mais caro:
  - O **Fast Drafter** (ex.: `qwen3-8-flash` ou `qwen-code-fast` local) tenta gerar a solução.
  - O código gerado é submetido imediatamente ao Code Judge determinístico.
  - Se aprovado, a tarefa conclui em fração do tempo e custo.
  - Se reprovado, a tarefa escala para o **Frontier Arbiter** (ex.: `gpt-5-6-luna-high` ou `gpt-6-astra`), garantindo 100% de sucesso final.

### 2. Torneio Empírico de Desempenho (Shadow Tournament)
- Avalia os Top 3 modelos em micro-tarefas canônicas da fábrica:
  - Transpilação e validação de AST.
  - Resolução de edge cases de UTF-8 no Windows.
  - Geração de fixtures e mocks para pytest.
- Calcula a taxa de **Pass@1 Empírico** e ajusta o **Elo Rating (Bradley-Terry)** de cada modelo.

### 3. Índice de Proximidade da Fronteira (Frontier Proximity Index - FPI)
- Modela a envoltória contínua da fronteira de Pareto em espaço normalizado log-linear.
- Categorias de modelos:
  - **Frontier Leader (FPI = 100%)**: Define a curva ótima de custo-benefício.
  - **Near-Pareto Challenger (FPI >= 95%)**: Modelos a poucos passos da fronteira com alta oportunidade (ex.: altíssimo throughput ou janela de contexto massiva).
  - **Dominated (FPI < 85%)**: Modelos superados em custo e qualidade por alternativas superiores.

---

## 🛠️ Comandos de CLI (`core.benchmarks.cli`)

### 1. Analisar Distância da Fronteira e Epsilon-Gap
```bash
python -m core.benchmarks.cli proximity
```
Exibe a tabela completa com FPI (%), $\epsilon$-gap de capacidade (pts), $\epsilon$-gap de custo (\$), Opportunity Score e classificação (Pareto Optimal vs Near-Challenger).

### 2. Listar os Top 3 Candidatos por Tier
```bash
python -m core.benchmarks.cli top3 --complexity high
python -m core.benchmarks.cli top3 --complexity critical
python -m core.benchmarks.cli top3 --complexity local_fast
```

### 3. Executar Corrida Especulativa
```bash
python -m core.benchmarks.cli race --complexity high --prompt "Implementar função com type hints e docstring"
python -m core.benchmarks.cli race --complexity critical
```

### 4. Rodar Torneio Empírico e Atualizar Elo Leaderboard
```bash
python -m core.benchmarks.cli tournament --complexity high
```

---

## 🏛️ Governança Inviolável

1. **Zero Data-Sharing**: Endpoints com retenção ou compartilhamento de dados (`contributor`, `data-sharing`) são banidos de todas as corridas especulativas.
2. **Local-First ($0) Sempre Disponível**: O tier `local_fast` disputa exclusivamente entre instâncias locais do Ollama (`qwen-code-deep`, `qwen-code-fast`, `deepseek-coder`).
3. **Portão Determinístico Obrigatório**: Nenhuma vitória especulativa é concedida sem aprovação no Code Judge (`py_compile` ou testes executáveis).
