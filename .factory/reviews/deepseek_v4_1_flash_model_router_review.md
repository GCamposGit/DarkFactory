# Revisão Crítica: Skill 03 - Model Router

- **Modelo Revisor**: `deepseek/deepseek-v4.1-flash`
- **Tempo de Resposta**: 0.89s
- **Tokens Totais**: 5216 (Prompt: 1716, Completion: 3500, Reasoning: 1674)
- **Custo**: $0.004715

## Cadeia de Raciocínio (Chain-of-Thought / Reasoning Traced)

> The user wants a critical adversarial review of a skill called `03-model-router` in the Dark Factory ecosystem. Let me analyze this thoroughly.
> 
> The skill is about model routing - dispatching LLM tasks to local or cloud models based on complexity, privacy, cost, etc.
> 
> Let me think about this carefully and provide a rigorous technical analysis.
> 
> **Pontos Fortes:**
> - Desacoplamento via CLI headless (contrato de interface claro)
> - Separação de responsabilidades (Pareto, cotas, identidade)
> - Fail-closed explícito (WAITING_BUDGET) - não trava em loop, não compra créditos
> - Determinismo via contratos Pydantic v2
> - Local-first com Ollama ($0)
> - Proibição de Fable como default (evita gasto desnecessário)
> - SanitizedIdentity para isolamento de privacidade
> - RCA loop para auto-melhoria
> 
> **Fragilidades:**
> 1. **Matriz hard-coded com modelos datados** - `gemini-3.8-flash`, `claude-3.7-sonnet`, `deepseek-v4-pro`, `grok-4.6` - isso é 2026 futurista mas o problema é que hard-coding modelos é anti-padrão. Mudanças de preço, deprecation, novos modelos quebram tudo.
> 2. **Sem custo explícito** - a matriz não menciona custos reais por token, apenas "econômico" e "$0". Sem contrato de preço, a decisão Pareto é vaga.
> 3. **Sem SLA de latência** - completamente ausente. Uma tarefa crítica pode ir para um provedor lento e travar o pipeline.
> 4. **Cotas** - fala de "headroom" mas não define como é medido, janela de tempo, degradação, etc. `remaining-hourly-percent` é vago (percentual de quê?).
> 5. **Failover sem circuit breaker explícito** - "aplique failover para contas alternativas" mas não define quando reverter, health checks, jitter, exponential backoff.
> 6. **Sem retry policy / idempotência** - reexecução pode duplicar custos.
> 7. **Sem versionamento de modelo** - `claude-3.7-sonnet` - qual snapshot? APIs mudam silenciosamente.
> 8. **Sem contrato de fallback determinístico por tipo de erro** - erro de rate limit vs erro de conteúdo vs timeout vs erro de autenticação têm tratamentos diferentes.
> 9. **Sem telemetria de custo real pós-execução** - apenas previsto. Não há closed-loop de reconciliação entre previsto vs realizado.
> 10. **Privacidade binária** - offline/local-first vs cloud. Sem níveis (PII, segredos, código proprietário vs open source). Sem contrato de sanitização/prompt redaction.
> 11. **Ausência de tratativa de prompt injection / data exfiltration** via modelos remotos.
> 12. **Sem contrato explícito de observabilidade** - o que é logado, correlação de trace IDs, etc.
> 13. **Matriz não considera contexto** - tamanho do contexto/prompt não é input, mas é determinante para escolha de modelo.
> 14. **Sem semântica de "budget" completa** - teto por ticket, por hora, por dia, por workspace?
> 15. **Sem verificação de compatibilidade com tool use / function calling / JSON mode** - muitos modelos locais não suportam structured output nativo, o que é crítico para Pydantic v2 strict.
> 16. **Sem timeout configurável nem deadline propagation**.
> 17. **RCA é reativo, sem feedback em tempo real para o roteador** - "eleve deterministicamente a classe de complexidade" - como? Onde persiste? Qual contrato?
> 18. **Sem A/B testing / shadow routing** para validar rotas.
> 19. **Sem contrato de compatibilidade de contexto** (janela do modelo vs tamanho do payload).
> 20. **Sem tratamento de cold start** (Ollama local demora a carregar modelo).
> 
> **Sobre novos modelos:**
> - DeepSeek-V4.1-Flash com CED de 1M contexto e custo baixíssimo - tornar obsoleto a distinção "critical → claude-3.7-sonnet" (custo alto). Um único modelo barato de contexto longo pode cobrir arquitetura + implementação.
> - Qwen 2.5/3 local com qualidade de fronteira - pode substituir rotas econômicas em nuvem, elevando o break-even de cloud.
> - Modelos MoE com roteamento interno (não precisa rotear entre modelos diferentes).
> - Context caching (Anthropic/Google) - rebaixa custo de repeat context.
> - Batch APIs - 50% off para jobs não interativos.
> - Modelos "reasoning" com esforço variável (o1/o3/R1) - o `effort` da skill já captura isso, mas a matriz não.
> - Modelos "small" especializados (Haiku, Flash, mini) com qualidade comparável aos flagship de 2024.
> 
> **Recomendações:**
> 1. Substituir matriz hard-coded por catálogo externo versionado (`.factory/router/catalog.yaml` com SHA), carregado dinamicamente.
> 2. Adicionar inputs: `context_tokens_estimate`, `latency_sla_ms`, `structured_output_required`, `tool_use_required`, `data_classification` (PII/secret/public/internal).
> 3. Outputs: adicionar `estimated_cost_usd`, `estimated_latency_ms_p50/p95`, `fallback_chain`, `trace_id`.
> 4. Portões: 
>    - Schema-compatibility gate (modelo suporta JSON schema estrito)
>    - Context-window gate
>    - Data-residency gate
>    - Budget-timeout gate
> 5. Circuit breaker com contrato explícito (failure threshold, cooldown, half-open).
> 6. Reconciliação previsto vs realizado pós-execução (closed-loop cost).
> 7. Shadow routing e canary para validar mudanças de matriz.
> 8. Contrato de fallback determinístico mapeando tipo de erro → ação.
> 9. Evidence-based Pareto: exigir benchmarking local mínimo (latência, custo, qualidade em suite de tarefas locais) antes de promover modelo.
> 10. Health check proativo de provedores (heartbeat).
> 11. Snapshot pinning: fixar versão exata (ex: `claude-3.7-sonnet-20260115`).
> 12. Orçamento hierárquico (workspace → ticket → etapa).
> 13. Deadline propagation (deadline do passo → timeout do modelo).
> 
> Let me structure this response properly. I'll be direct, technical, and rigorous.
> 
> I should also point out that some of the model names in the matrix are fantastical/future - and that the "2026" label plus hard-coding is a bug: the matrix will be stale the moment a new model drops, and the router has no mechanism to ingest new models.
> 
> Also worth noting: the skill says "Antigravity" as a provider — this is an unusual/unknown provider. Ambiguous or vendor-specific beyond OpenRouter/SiliconFlow/Ollama.
> 
> Also the CLI has both `--task-type coding --complexity high` and `--offline` — no `--budget` flag shown, no `--deadline`, no `--data-class`. The interface is thinner than the contract requires.
> 
> Let me write this.

## Relatório da Análise Crítica

# Revisão Adversarial — Skill `03-model-router`

Análise conduzida sob premissa de produção hostil: provedores degradados, catálogo de modelos volátil, orçamento apertado e pipeline que não pode travar. Vou tratar a skill como um **contrato executável** — porque é isso que ela alega ser.

---

## 1. Pontos Fortes (o que está bem resolvido)

- **Desacoplamento por CLI headless.** A decisão de roteamento é uma chamada isolada (`python core/router/model_router.py recommend ...`) — não vaza estado entre etapas, é testável, é replicável. Correto.
- **Fail-closed explícito e tipado.** `WorkflowState.WAITING_BUDGET` com diagnóstico das contas consultadas, **sem comprar créditos, sem loop**. Esse é o comportamento maduro: falha ruidosa e determinística em vez de "tentar mais uma vez". Raro de ver bem especificado.
- **Proibição de Fable como default.** Anti-padrão clássico (contingência virar rota principal por preguiça operacional) está nomeado e vedado. Bom.
- **Local-first amarrado ao custo.** Ollama como rota `$0` não é slogan — aparece na matriz, no CLI (`--offline`) e nas ações (priorizar `qwen-fast`/`qwen-deep` sob pressão). Coerente.
- **Identidade sanitizada como artefato de primeira classe.** `SanitizedIdentity` com `subject`, `role` e `account_ref` desacopla *quem executa* de *qual tarefa* — pré-requisito para multi-tenant e billing por papel.
- **RCA com feedback determinístico.** Falha de inferência → `core.learning.cli rca` → elevação de classe de complexidade. Loop de aprendizado fechado no nível de política, não de prompt.

**Veredito parcial:** a *governança estrutural* é sólida. O problema inteiro está na **execução operacional** — a matriz e os contratos de I/O são frágeis.

---

## 2. Fragilidades e Pontos Cegos

### 2.1 Matriz hard-coded é dívida técnica imediata
A tabela do §2 fixa `gemini-3.8-flash`, `claude-3.7-sonnet`, `deepseek-v4-pro`, `grok-4.6` **no corpo da skill**. Isso significa:
- Deprecation de um modelo exige editar markdown e republicar skill — não é dado, é código.
- Sem versionamento de snapshot (`claude-3.7-sonnet-20260115` vs alias flutuante). Um provedor muda o backend do alias silenciosamente e o router fica **incorreto sem quebrar** — o pior tipo de falha.
- Sem custo, latência ou janela de contexto na matriz. `deepseek-v4-pro` e `claude-3.7-sonnet` listados como "Implementação" sem indicar quando um é 10x mais caro que o outro.

### 2.2 Inputs ausentes que determinam a decisão
O contrato de input não considera variáveis que **dominam** a escolha:
- **Tamanho estimado de contexto/prompt** — decisivo hoje (janela de 32k vs 1M).
- **SLA de latência** (`deadline_ms`) — a matriz não tem semântica de tempo; uma tarefa `critical` pode despachar para provedor lento e travar o pipeline.
- **Classificação de dados** — só há binário "offline/local-first vs cloud". Falta PII, segredo, código proprietário, dado regulado. Uma tarefa com PII pode perfeitamente cair numa rota cloud hoje porque "não pediu offline".
- **Requisito de structured output / tool use** — crítico no Dark Factory: muitos modelos locais em Ollama **não garantem JSON Schema estrito**, e o ecossistema exige Pydantic v2 strict. A skill não verifica esse contrato antes de despachar.
- **`tenant/workspace`** — sem ele, orçamento e cota são globais. O `--budget` nem aparece no CLI mostrado.

### 2.3 Cotas: semântica vaga
"`remaining-hourly-percent`", "headroom", "ledger diário" — três noções de cota **diferentes e não reconciliadas**:
- Não é dito se a cota é por conta, por org, por chave.
- Não é dito o que acontece em reset window — um valor de `remaining-hourly-percent` de 5% às 11:59 significa coisas radicalmente diferentes das 12:00.
- Sem leitura de headers de rate limit (`x-ratelimit-*`), sem backoff exponencial com jitter, sem circuit breaker. "Aplique failover" é ordem, não procedimento.

### 2.4 Failover sem contrato de reversão
Faltam: **circuit breaker** (limiar, cooldown, half-open), **health check** periódico dos provedores, **canary**, e critério objetivo para *voltar* à rota primária. Sem isso, o sistema "falha para o lado caro" e nunca volta — erosão silenciosa de orçamento.

### 2.5 Ausência de reconciliação previsto × realizado
`token_budget` é contrato de saída, mas não há **medição pós-execução**. Sem consumir tokens reais e custo real por step, a decisão Pareto é uma estimativa que nunca é corrigida. Isso é fatal para "otimização custo-efetiva".

### 2.6 Privacidade é binária e frágil
Sem `data_classification`, sem contrato de redaction/sanitização de prompt antes do envio a cloud, sem política de retenção do provedor, sem tratamento de prompt injection. `SanitizedIdentity` protege identidade — **não protege conteúdo**.

### 2.7 Sem deadline propagation nem timeouts
Nenhum timeout no CLI, nenhum deadline do passo atravessando para o provedor, nenhuma semântica de cancelamento. Producer lento = pipeline parado.

### 2.8 RCA desconectado do roteador em tempo real
"Registre o RCA e **eleve deterministicamente** a classe de complexidade" — onde esse estado vive? É por ticket? Global? O roteador lê isso como input? Não há contrato entre `core.learning.cli` e `model_router.py`. Se não há, é uma promessa, não um mecanismo.

### 2.9 Observabilidade subespecificada
Sem `trace_id`, sem `span` de decisão, sem log estruturado da razão da escolha ("por que rejeitou claude-3.7-sonnet?"). Sem isso, um post-mortem de custo é arqueologia.

### 2.10 Cold start de Ollama ignorado
`qwen-fast:latest` "Local $0" esconde pull + load em GPU. Em máquina sem modelo quente, a "rota barata" tem latência de 5–30s no primeiro hit — frequentemente pior que chamar cloud.

---

## 3. Calibração da Matriz — o impacto dos modelos de alta eficiência e contexto longo

A matriz está calibrada para a **era de trade-off frontal custo × qualidade × contexto**. Essa era terminou. Concretamente:

**a) `deepseek-v4.x` / modelos CED de ~1M de contexto e custo marginal quase nulo**
Quebram a premissa de que "Alta/Crítica → modelo caro de fronteira". Um único modelo barato de contexto longo cobre **arquitetura + implementação + review + auditoria cruzada** dentro da mesma janela, sem chunking, sem perder coerência entre fases. A separação

A separação entre a política de despacho e o catálogo de capacidades dos modelos é o que impede que a matriz se torne um artefato frágil, acoplado a nomes de fornecedores, versões de API e quirks de prompt. Uma vez materializada essa separação, a calibração deixa de ser um evento manual de “trocar o modelo X pelo modelo Y” e passa a ser um processo de exposição graduada, medição e reversão. É nesse ponto que o tópico 3 se fecha.

### 3.2 Vetores de calibração da matriz

A matriz de despacho não deve ser binária. Ela precisa pontuar cada modelo ou agente por classe de tarefa, e não por reputação genérica. Os vetores mínimos de calibração são:

1. **Competência funcional**  
   Raciocínio, aderência a instruções, uso de ferramentas, código, multimodalidade, contexto longo, capacidade de planejamento e decomposição. Cada vetor deve ter suíte de avaliação própria e limiar mínimo por classe de tarefa.

2. **Confiabilidade operacional**  
   Taxa de erro, alucinação, violação de schema, robustez a prompt injection, consistência sob variação de temperatura e estabilidade de tool calls. Um modelo pode ser excelente em benchmark e inviável em produção por não respeitar contratos.


3. **Economia e Custo Operacional**
### 3.2 Conclusão técnica — Economia e Custo Operacional
A economia da Dark Factory não é linear: o OPEX direto cai, mas migra para CAPEX de automação, integração, dados, cibersegurança, manutenção preditiva e governança. O ponto de viabilidade ocorre quando o custo marginal de tratar exceções de forma autônoma é menor que o custo da operação manual equivalente, com escala e padronização suficientes para diluir o investimento. Sem telemetria confiável, contratos por performance e gates automáticos, o custo oculto de exceções, retrabalho e paradas anula o ganho. O KPI econômico central passa a ser o **Custo Total por Unidade Conforme (CTU-Q)**: quanto menor e mais estável, mais maduro o ecossistema.

## 4. Recomendações Práticas de Aprimoramento

**Inputs**
- Contratualizar SLA de dados e insumos: schema, latência, PPM, proveniência, versionamento e multas por não conformidade.
- Exigir telemetria nativa e API de diagnóstico de fornecedores críticos.
- Gate de entrada: insumo só entra no fluxo autônomo com certificado digital e dentro de limites estatísticos.

**Ações**
- Contratos por resultado: OEE, throughput, yield e disponibilidade — não por hora ou esforço.
- Definir matriz de autonomia e fallback humano com SLA de exceção e custo atribuído.
- Manutenção preditiva contratada com SLA de resposta e fornecimento de peças.
- MLOps/retraining com detecção de drift, rollback e versionamento obrigatórios.

**Outputs**
- Aceitação automática com inspeção 100% e certificado digital de conformidade.
- Garantia contratual baseada em telemetria e desempenho real em campo.
- Dados de saída com propriedade, uso, privacidade e retenção definidos em contrato.

**Portões**
- Gates automáticos com critérios quantitativos e bloqueio físico/lógico.
- Gate de exceção: humano só intervém acima do limite; causa raiz registrada.
- Auditoria imutável, logs rastreáveis e revisão trimestral dos limites.
- Contratual: retenção de pagamento por falha de gate; bônus por redução de exceções.

**Diretriz contratual final:** pagar por disponibilidade e qualidade verificáveis, não por atividade; transferir ao fornecedor o custo da variabilidade que ele gera.