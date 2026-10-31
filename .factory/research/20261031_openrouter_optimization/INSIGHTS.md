# Dossiê de Pesquisa Arquitetural: Otimização de Uso OpenRouter vs. Subscrições na Dark Factory

- **Slug**: `20261031_openrouter_optimization`
- **Data**: 31 de Outubro de 2026
- **Contexto**: USR-440 (Frente 5 / Plano V0)
- **Status**: Validado para implementação

---

## 1. Fundamentação Teórica e Literatura Canônica

### 1.1 FrugalGPT e Cascatas de Decisão (Zaharia et al., arXiv:2305.05176)
O princípio do FrugalGPT demonstra que uma fábrica autônoma operando em regime de alta densidade de tarefas não deve delegar indistintamente todas as etapas a modelos de fronteira monolíticos (como Claude Opus ou GPT-4/o). 
A estratégia ótima divide o fluxo em 3 técnicas:
1. **Model Cascade**: Tarefas de menor complexidade (refatorações mecânicas, documentação, testes unitários simples, linting) são resolvidas por modelos com custo de inferência ordens de grandeza inferior ($0.002/tarefa vs $0.20/tarefa).
2. **Model Router**: Classificação prévia da demanda com base em heurísticas estruturadas (complexidade do ticket, diff esperado, risco arquitetural).
3. **LLM Score/Judge Fallback**: Se o modelo de custo inferior não atingir o critério no portão determinístico local (`setup + validate`), a cascata escala para o modelo de capacidade superior.

### 1.2 RouteLLM (LMSYS / UC Berkeley)
Modelos de roteamento baseados em dados de preferência e fronteiras de Pareto demonstram que desviar até 75% das requisições para modelos mais eficientes retém mais de 95% do benchmark de acerto de modelos de fronteira, reduzindo a conta de inferência agregada em até 74%.

---

## 2. A Dinâmica Econômica da Fábrica: Custo Fixo (Sunk Cost) vs. Custo Marginal por Token

Na Dark Factory, o balanceamento financeiro segue a regra de dois regimes:

### 2.1 Regime de Abundância de Cotas (Headroom > 15%)
- As assinaturas básicas (Claude Pro, ChatGPT Plus/Codex, SuperGrok) possuem custo mensal pré-pago fixo (~US$ 20/mês cada).
- O custo marginal de inferência dentro das janelas móveis saudáveis (> 15%) é **US$ 0,00 por token**.
- **Diretriz**: Enquanto as cotas das assinaturas estiverem saudáveis, a esteira deve consumir prioritariamente o headroom das assinaturas para fatias complexas, pois o valor já foi amortizado.
- **Exceção de Pareto**: Tarefas de baixíssima complexidade (ex.: geração de resumos, checagens de sintaxe, micro-fatias) podem ser pontualmente executadas via OpenRouter (ex.: DeepSeek V4.1 Flash a US$ 0,002/tarefa) se isso evitar bater precocemente no teto semanal das assinaturas.

### 2.2 Regime de Contingência e Fallback Crítico (Headroom <= 15%)
- Quando os provedores atingem o piso de segurança ($\le 15\%$), a esteira headless anteriormente congelava em `route_wait` ou modo `DEGRADED`.
- **Diretriz**: O OpenRouter atua como **garantia de continuidade operacional (Zero-Stall)**:
  - **Planejamento**: Deve selecionar modelo de alta inteligência disponível no OpenRouter (ex.: Claude 3.5 Sonnet ou GPT 4o / DeepSeek Chat / Qwen 2.5 Coder 32B). <!-- census: wire-id -->
  - **Desenvolvimento**: Deve atuar com capacidade de escrita (`write`), aplicando as modificações no `req.cwd` através de blocos de patch estruturados validados deterministicamente pelo portão de testes.
  - **Governança de Orçamento**: O consumo é rigorosamente limitado pelo teto por run (`openrouter_usd: 2.0`) e teto mensal (`10.0 USD`), emitindo notificação de alerta crítico via Telegram caso a verba atinja o limiar de 80%.

---

## 3. Matriz de Decisão Arquitetural para o USR-440

| Estágio | Comportamento Anterior | Comportamento Otimizado (USR-440) | Justificativa |
| :--- | :--- | :--- | :--- |
| **Planejamento** | `openrouter_model: distill.openrouter` (modelo barato sem tier alto) | Modelo de alta capacidade (`planning.openrouter`) configurado e validado | Garantia de planos consistentes e fatiamento técnico de qualidade Staff+ |
| **Desenvolvimento** | `openrouter_ok: false`, `HARNESS_CAPABILITIES["openrouter"] = {"read"}` | `openrouter_ok: true` como fallback quando cotas estão críticas; `write` implementado via parser de patches no workspace | Evita paralisação da fábrica autônoma na exaustão de cotas de assinatura |
| **Sentinela / Frota** | Relata `Frota: NENHUMA` quando Claude, Codex e Grok $\le 15\%$ | Relata rota OpenRouter viável quando houver saldo em dólar dentro do orçamento | Transparência real sobre viabilidade de execução autônoma |
| **Teto de Gastos** | Apenas `openrouter_usd` por execução (2.0 USD) | Verificação combinada de saldo pré-pago, teto por execução e aviso Telegram em >80% do teto mensal | Proteção financeira total do Owner |
