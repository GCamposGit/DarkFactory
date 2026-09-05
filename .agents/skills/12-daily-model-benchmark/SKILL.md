---
name: daily-model-benchmark
description: Executa a varredura e benchmarking diário de modelos de linguagem de fronteira (OpenAI, Anthropic, Google, xAI, Meta, DeepSeek, Qwen, GLM, Kimi). Roda no máximo uma vez por dia ao disparar qualquer processo da Dark Factory. Coleta métricas do Artificial Analysis e preços do OpenRouter, calculando a Fronteira de Eficiência de Pareto para direcionar decisões de roteamento ótimo.
---

# Daily Model Benchmark & Pareto Efficiency Frontier (Dark Factory)

Esta skill governa o monitoramento contínuo do ecossistema global de modelos de inteligência artificial, atualizando diariamente a superfície de custo-benefício para que a Dark Factory sempre despache tarefas para os modelos mais eficientes do mercado.

## Regra de Ouro da Execução Diária

- **Frequência**: No máximo **1 vez por dia** (`YYYY-MM-DD`).
- **Comportamento Idempotente**: Ao ser chamado por qualquer processo da Dark Factory (seja no `model_router.py`, na máquina de estados `state.py` ou no launcher do `DarkHub`), o sistema verifica se a data de hoje já consta no ledger `.factory/benchmarks/latest.json`.
- **Custo Computacional**: Se já rodou hoje, retorna instantaneamente em **<1ms** sem realizar nenhuma requisição de rede.

## Fontes de Dados e Metodologia

1. **Artificial Analysis (`artificialanalysis.ai`)**:
   - **Coding Agent Index**: Avaliação de software engineering real (DeepSWE, Terminal-Bench v2.1, SWE-Atlas-QnA) com Pass@1.
   - **Intelligence Index v4.2**: Avaliação composta de raciocínio, agentes, matemática e ciência.
   - **Métricas de Performance**: Velocidade de saída (tokens/s), latência TTFT (s), tempo de execução e tokens por tarefa.
   - Suporte a `ARTIFICIAL_ANALYSIS_API_KEY` com fallback para catálogo calibrado.

2. **OpenRouter Public API (`openrouter.ai`)**:
   - Consulta pública ao vivo de catálogo sem autenticação.
   - Preços em tempo real por 1M tokens de prompt e completion.
   - Detecção de novos lançamentos de fabricantes (OpenAI, Anthropic, Google, Meta, DeepSeek, Qwen, Mistral, xAI).

3. **Fronteira de Pareto (Custo-Benefício)**:
   - Um modelo pertence à Fronteira de Pareto se nenhum outro modelo for simultaneamente **mais barato por tarefa** e de **maior capacidade**.
   - A Dark Factory utiliza essa fronteira para escolher o modelo mais eficiente para cada classe de complexidade de código (`high`, `medium`, `low`).

## Comandos CLI

```bash
# Executar ou verificar status do benchmark diário
python -m core.benchmarks.cli run

# Forçar atualização manual
python -m core.benchmarks.cli run --force

# Exibir status da execução e modelos recomendados
python -m core.benchmarks.cli status

# Visualizar tabela da Fronteira de Pareto de Codificação
python -m core.benchmarks.cli frontier

# Listar todos os modelos monitorados com preços e notas
python -m core.benchmarks.cli table

# Consultar recomendação ótima para uma tarefa
python -m core.benchmarks.cli recommend --task-type coding --complexity high
```

## Integração REST API (DarkHub)

- `GET /api/benchmarks/latest`: Retorna o snapshot completo do ledger diário.
- `GET /api/benchmarks/frontier`: Retorna apenas os modelos na Fronteira de Eficiência.
- `POST /api/benchmarks/refresh`: Força re-execução do benchmark sob demanda.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **RCA em Drift e Degradação de Modelos**:
   - Se um modelo de fronteira anteriormente estável começar a falhar em benchmarks de raciocínio ou aumentar tempo de resposta bruscamente, o benchmark detecta anomalia e registra no RCA.
   - O modelo é rebaixado na Fronteira de Pareto e as tarefas são redirecionadas para o próximo modelo eficiente.
2. **One-Shot em Roteamento de Engenharia**:
   - Fornece instantaneamente a melhor recomendação para o `model_router.py` sem chamadas de rede adicionais, assegurando decisões determinísticas e imediatas no início de cada tarefa.
3. **Extrapolação de Eficiência**:
   - Dados de eficiência token/custo informam as heurísticas de contexto e pruning do `08-meta-skills-evolver`, maximizando a produtividade sem inflar orçamentos de inferência.

