# Guia de Seleção & Benchmark de Modelos (2026 Edition)

Este guia consolida o benchmark técnico, a política de roteamento e a estratégia de custo-benefício para operar agentes de codificação em máxima eficiência.

---

## 1. O Cluster Local (Ollama - Custo Zero & Latência Zero)

Detectamos e configuramos nativamente o cluster local em `http://localhost:11434`:

### 1. `qwen-fast:latest` (Base: Qwen3-Coder-30B MoE A3B)
- **Especificações**: 30.5B parâmetros, quantização Q3_K_M, 8 threads, contexto 4.096, temperatura 0.15.
- **Caso de Uso**: Execuções ultrarrápidas, geração de código boilerplate, regex, schemas JSON, conversão de formatos e testes unitários simples.
- **Vantagem**: Segue instruções literais sem preâmbulos, custo $0, 0ms de latência de rede.

### 2. `qwen-deep:latest` (Base: Qwen3-Coder-30B MoE A3B)
- **Especificações**: 30.5B parâmetros, quantização Q3_K_M, 20 threads, contexto 16.384, temperatura 0.2.
- **Caso de Uso**: Lógica algorítmica local, refatorações internas em arquivos médios, manipulação de código confidencial sem tráfego de dados para a nuvem.

### 3. `gpt-review:latest` (Base: gpt-oss:20b)
- **Especificações**: 20.9B parâmetros, MXFP4, 20 threads, contexto 16.384, temperatura 0.1.
- **Caso de Uso**: Auditoria Nível 1 local. Atua como subagente independente de controle de qualidade inspecionando diffs gerados antes de subir para a nuvem.

---

## 2. Modelos de Fronteira em Nuvem

### 1. Gemini 3.8 Flash (Motor Central do Antigravity)
- **Papel**: Orquestrador Geral e Ingestor Massivo de Código.
- **Destaque**: Janela de contexto de 1M a 2M tokens com leitura ultra-barata (graças ao cache de contexto). Capaz de ler repositórios inteiros, documentações extensas e histórico de commits em uma única passada.

### 2. Grok 4.6 (xAI Subscription)
- **Papel**: Pesquisador em Tempo Real e Solucionador de Long-Horizon Tasks.
- **Destaque**: Benchmark de ponta em pesquisa de conhecimento técnico e resolução de problemas em menos turnos (~53 turnos vs 100+ de modelos concorrentes). Ideal para depuração de erros obscuros e pesquisa de documentações recentes de bibliotecas.

### 3. Claude 3.7 Sonnet (Extended Thinking) / Opus
- **Papel**: Arquiteto Chefe e Refatorador Cirúrgico.
- **Destaque**: Liderança comprovada em SWE-bench e raciocínio causal estrito. Ideal para fatiamento de épicos, definição de interfaces rígidas e PRDs onde ambiguidades custam caro.

### 4. Claude Haiku 5.5 (`claude-haiku-5-5`)
- **Papel**: executor econômico de fatias mecânicas e bem especificadas, como subagente de desenvolvimento do harness (e destilador de testes em `.claude/agents/test-runner.md`). Política implementada em `core/line/subagent_routing.py` com parâmetros em `.factory/config/subagent_model_routing.json`; não altera o cascade da esteira (`line_routing.json`).
- **Quando usar** (TODAS as condições): tipo mechanical_edit (rename, format, lint, typing), docs_sync, test_from_spec, config_data, boilerplate_from_template, test_run_distill ou ledger_update; complexidade `low` (`medium` só em docs_sync e test_run_distill); no máximo 3 arquivos e 150 linhas alteradas; critério de aceite executável e rápido já definido pelo planejador; ambiguidade resolvida (Gate G1); nenhuma falha anterior do Haiku na tarefa.
- **Quando NÃO usar**: caminhos protegidos por governança; tags de risco (security, credentials, auth, payments, concurrency, locking, transactions, migration, data_deletion, public_contract, routing, quota, flaky_test, root_cause_debug, architecture); estágios planning, grill, review e integration (Haiku nunca planeja, revisa nem resolve conflito de integração); tarefas sem aceite executável, com ambiguidade pendente ou acima dos limites. Nesses casos vale o Sonnet (`claude-sonnet-5-5`) e, em planning, o Opus (`claude-opus-5-5`).
- **Escalonamento**: uma única tentativa com Haiku. Se o portão focado falhar ou a revisão apontar defeito de correção, nova tentativa com Sonnet (sem iterar 3 vezes com Haiku). Haiku nunca escala sozinho para Opus nem Fable. Piso de cota de 15% (mesma conta Anthropic), revisão em família diferente do implementador e portão único `runner.py --quick` permanecem.

### 5. Modelos Chineses de Alto Rendimento (DeepSeek & Qwen)
- **DeepSeek-R1**: O modelo de raciocínio lógico/matemático com o melhor custo-benefício do planeta. Ideal para validação de algoritmos, geração de testes de mutação e auditoria adversarial profunda.
- **DeepSeek-V4 Pro**: Especialista em código com precificação de frações de centavo por milhão de tokens.
- **Qwen3-Max / Qwen3-Coder**: O ápice da engenharia aberta chinesa para refatoração e transformações de código em lote.

---

## 3. Hubs de API & Repositórios Recomendados

Para acessar os modelos de ponta chineses e globais com facilidade e economia:

| Hub / Provedor | Modelos Oferecidos | Diferencial Principal | URL |
| :--- | :--- | :--- | :--- |
| **SiliconFlow (硅基流动)** | DeepSeek R1/V4, Qwen3, GLM-4 | **Altamente Recomendado**: API compatível com OpenAI, aceleração de hardware, tier gratuito para modelos leves e preços em centavos. | [siliconflow.com](https://siliconflow.com) |
| **DeepSeek Platform** | DeepSeek V4 Pro, DeepSeek R1 | Acesso direto na fonte com Context Caching ativo (desconto de até 90% em tokens lidos). | [platform.deepseek.com](https://platform.deepseek.com) |
| **OpenRouter** | Claude 3.7, Grok 4.6, DeepSeek, Qwen, GPT-5 | Uma única chave para todos os modelos com fallback automático entre provedores em caso de instabilidade. | [openrouter.ai](https://openrouter.ai) |
| **Alibaba Cloud DashScope** | Família Qwen (Qwen-Max, Qwen-Plus, Qwen-Coder) | Plataforma nativa com a maior capacidade de throughput para os modelos Qwen. | [dashscope.aliyun.com](https://dashscope.aliyun.com) |

## Registro de modelos (gerado)

Gerado de `.factory/config/model_registry.json` por `python scripts/generate_model_docs.py`; nao edite entre os marcadores.

### Papeis

<!-- BEGIN model-registry:roles -->
| Papel | Modelo (id) | Status |
| --- | --- | --- |
| `development.claude` | `claude-sonnet-5-5` | active |
| `development.codex` | `gpt-6-luna` | active |
| `development.grok` | `grok-4.7` | active |
| `distill.openrouter` | `deepseek/deepseek-v4.1-flash` | active |
| `planning.claude` | `claude-opus-5-5` | active |
| `review.codex` | `gpt-6-luna` | active |
| `review.grok` | `grok-4.6` | active |
| `subagent.haiku` | `claude-haiku-5-5` | active |
| `subagent.opus` | `claude-opus-5-5` | active |
| `subagent.sonnet` | `claude-sonnet-5-5` | active |

Pares implementador -> revisor (familias distintas): `development.claude` -> `review.codex`; `development.codex` -> `review.grok`; `development.grok` -> `review.codex`

Piso de cota: 15%. Marcadores proibidos em papel autonomo: `fable`, `claude-fable`, `astra`, `gpt-6-astra`.
<!-- END model-registry:roles -->

### Catalogo

<!-- BEGIN model-registry:catalog -->
| Modelo (id) | Provedor | Familia | Faixa | Aliases | Status | Entrada US$/Mtok | Saida US$/Mtok | Contexto |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-3-7-sonnet` | anthropic | claude | mid | `claude-legacy-sonnet`, `legacy-sonnet` | deprecated | - | - | 200000 |
| `claude-fable-5-1` | anthropic | claude | frontier | `fable` | candidate (proibido autonomo) | 3.0 | 15.0 | 200000 |
| `claude-haiku-5-5` | anthropic | claude | low | `haiku` | active | - | - | - |
| `claude-opus-5` | anthropic | claude | high | `opus-legacy` | deprecated | 5.0 | 25.0 | 200000 |
| `claude-opus-5-5` | anthropic | claude | high | `opus` | active | - | - | - |
| `claude-sonnet-5-5` | anthropic | claude | mid | `sonnet` | active | - | - | - |
| `deepseek-coder` | deepseek | deepseek | low | `coder-legacy` | deprecated | - | - | - |
| `deepseek/deepseek-r1` | deepseek | deepseek | high | `deepseek-r1` | deprecated | - | - | 128000 |
| `deepseek/deepseek-v4-pro` | deepseek | deepseek | high | `deepseek-v4-pro` | candidate | - | - | 256000 |
| `deepseek/deepseek-v4.1-flash` | deepseek | deepseek | low | `deepseek-flash` | active | 0.15 | 0.6 | 1048576 |
| `gemini-2.0-flash-001` | google | gemini | low | `gemini-legacy-flash` | deprecated | - | - | 1000000 |
| `gemini-3.1-flash-lite-image` | google | gemini | low | `gemini-image-fast` | candidate | - | - | - |
| `gemini-3.1-pro` | google | gemini | high | `gemini-pro` | candidate | - | - | 1048576 |
| `gemini-3.8-flash` | google | gemini | low | `flash` | active | 0.75 | 3.75 | 1000000 |
| `gpt-6-astra` | openai | openai | frontier | `astra` | candidate (proibido autonomo) | 10.0 | 50.0 | 1050000 |
| `gpt-6-luna` | openai | openai | mid | `luna` | active | 0.2 | 1.2 | 1050000 |
| `gpt-image-2` | openai | openai | high | `gpt-image` | candidate | - | - | - |
| `gpt-sol` | openai | openai | high | `sol` | active | 2.5 | 10.0 | 500000 |
| `grok-4.6` | xai | grok | mid | `grok-review` | active | 2.0 | 8.0 | 524288 |
| `grok-4.7` | xai | grok | mid | `grok-dev` | active | - | - | - |
<!-- END model-registry:catalog -->

### Limiares do Grill

<!-- BEGIN model-registry:grill_thresholds -->
| Limite | Valor |
| --- | --- |
| Preco maximo de entrada (US$/Mtok) | `15` |
| Preco maximo de saida (US$/Mtok) | `75` |
| Faixas frontier | `frontier` |
| Grill para preco desconhecido fora de active | `true` |
| Etapas minimas de qualificacao | `3` |

`promote()` recusa a promocao com `WAITING_HUMAN` quando `requires_grill()` retorna motivos para a mudanca proposta.
<!-- END model-registry:grill_thresholds -->
