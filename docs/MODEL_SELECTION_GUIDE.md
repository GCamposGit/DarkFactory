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

### 4. Modelos Chineses de Alto Rendimento (DeepSeek & Qwen)
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
