---
name: daily-model-benchmark
description: Executa a varredura e benchmarking diário de modelos de linguagem de fronteira (OpenAI, Anthropic, Google, xAI, Meta, DeepSeek, Qwen, GLM, Kimi). Roda no máximo uma vez por dia ao disparar qualquer processo da Dark Factory. Coleta métricas do Artificial Analysis e preços do OpenRouter, calculando a Fronteira de Eficiência de Pareto para direcionar decisões de roteamento ótimo.
---

# 12 - Daily Model Benchmark & Fronteira de Pareto

Esta skill governa o monitoramento diário do ecossistema global de modelos de inteligência artificial, atualizando a fronteira de eficiência custo-benefício para subsidiar decisões de roteamento ótimo da Dark Factory.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Gatilho Temporal**: Execução diária automática (máximo 1 vez por dia, chave `YYYY-MM-DD`).
- **Fontes de Dados**:
  - API pública de modelos e preços do OpenRouter (`openrouter.ai`).
  - Índices do Artificial Analysis (`artificialanalysis.ai`) para engenharia de software e raciocínio.
- **Cache Local**: Snapshot anterior em `.factory/benchmarks/latest.json`.

### Ações e Procedimento Executável
1. **Verificação Idempotente Pré-Execução**:
   - Se o benchmark já tiver sido executado na data de hoje (`YYYY-MM-DD`), retorne instantaneamente em <1ms sem realizar chamadas de rede.
2. **Execução Headless via CLI**:
   ```bash
   # Executar ou verificar status do benchmark diário
   python -m core.benchmarks.cli run

   # Visualizar a Fronteira de Pareto de Codificação
   python -m core.benchmarks.cli frontier

   # Consultar recomendação ótima para uma classe de tarefa
   python -m core.benchmarks.cli recommend --task-type coding --complexity high
   ```
3. **Cálculo da Fronteira de Pareto**:
   - Um modelo pertence à Fronteira de Pareto se nenhum outro modelo for simultaneamente mais barato por tarefa e de maior capacidade comprovada.

### Outputs Estruturados
- **Ledger Diário de Benchmarks**: `.factory/benchmarks/latest.json` contendo catálogo consolidado, preços atualizados por 1M tokens e status da fronteira.
- **Insumos para o Model Router**: Dados consumidos diretamente por `core/router/model_router.py` para seleção de planejadores (`PlannerTier.HIGH`) e implementadores econômicos.

### Portões, Política e Validação
- **Comportamento Idempotente Obrigatório**: Proibida sobrecarga de requisições de rede se a execução do dia já foi concluída.
- **Resiliência a Falhas de Rede**: Em caso de indisponibilidade de APIs externas, o sistema mantém o último catálogo válido com timestamp visível, sem bloquear tarefas em andamento.
- **Autoridade de Roteamento**: Nenhuma tarefa pode assumir modelos fora da fronteira de Pareto sem justificativa técnica explícita.

---

## 2. Continuous Self-Improvement & RCA de Benchmarks

- **Detecção de Degradação de Modelos**: Se um modelo de fronteira sofrer aumento abrupto de latência ou regressão de raciocínio, o benchmark registra anomalia e o rebaixa na fronteira, alertando o `model-router`.
