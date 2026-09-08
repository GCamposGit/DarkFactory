---
name: model-router
description: Avalia tarefa, complexidade, privacidade, consumo previsto e cota disponível em todas as contas para despachar modelos locais ou de nuvem. Use ao iniciar uma etapa ou subagente e quando houver pressão de tokens, failover de conta ou decisão entre assinatura e API paga.
---

# Model Router: Roteamento Inteligente & Otimização de Recursos

Esta skill guia o agente e o desenvolvedor na seleção do modelo mais eficiente e de melhor custo-benefício para cada tarefa técnica.

## Utilização via Linha de Comando

O roteador está implementado em `core/router/model_router.py`:

```bash
# Consultar recomendação ótima para uma tarefa
python core/router/model_router.py recommend --task-type coding --complexity high

# Consultar modelo local (Ollama offline/privado)
python core/router/model_router.py recommend --task-type review --offline

# Listar modelos locais ativos no Ollama
python core/router/model_router.py list-local

# Invocar diretamente o executor rápido local (Custo $0)
python core/router/model_router.py call-local --model qwen-fast:latest --prompt "Gere o schema JSON para..."

# Prever consumo e simular pressão de uma cota horária
python core/router/model_router.py recommend --task-type coding --complexity high --task-description "Implementar parser tipado e testes" --expected-steps 4 --remaining-hourly-percent 18
```

## Pressão de tokens e continuidade

Toda recomendação deve considerar a previsão de tokens e, quando disponível, o relatório de todas as contas. O resultado `token_budget` é um contrato operacional: respeite o teto de saída, o esforço, o tamanho dos blocos, a prioridade e a indicação de adiar trabalho de fronteira.

Sob pressão, favoreça scripts e modelos locais, aceite maior latência e menor qualidade marginal, e entregue em módulos curtos. Quando outra conta tiver mais headroom, faça failover. Antes de consumir a reserva final das assinaturas, use um gateway de API paga configurado.

Para faixas, ordem de decisão e campos do contrato, leia [references/token-stress-policy.md](references/token-stress-policy.md).

## Regras de Despacho (Matriz 2026)

| Tipo de Tarefa | Complexidade | Modelo Eleito | Provedor / Endpoint |
| :--- | :--- | :--- | :--- |
| **Ingestão & Mapeamento** | Qualquer | `gemini-3.8-flash` | Antigravity Native |
| **Pesquisa Web / Docs Vivos** | Média / Alta | `grok-4.6` | xAI API |
| **Arquitetura & PRD** | Alta / Crítica | `claude-3.7-sonnet` ou `deepseek-r1` | Anthropic / SiliconFlow |
| **Implementação Cirúrgica** | Crítica / Alta | `claude-3.7-sonnet` | Anthropic / OpenRouter |
| **Implementação Cirúrgica** | Média | `deepseek-v4-pro` ou `qwen-deep` | SiliconFlow / Ollama |
| **Tarefas Rápidas / Boilerplate** | Baixa | `qwen-fast:latest` | Ollama (Local $0) |
| **Auditoria Nível 1** | Local | `gpt-review:latest` | Ollama (Local $0) |
| **Auditoria Nível 2** | Cruzada | `deepseek-r1` ou `grok-4.6` | SiliconFlow / xAI |

## Hubs de API Recomendados
1. **SiliconFlow (`siliconflow.com`)**: Agregador de alto desempenho com API compatível com OpenAI para DeepSeek V4/R1, Qwen3 e GLM.
2. **DeepSeek Platform (`platform.deepseek.com`)**: API direta com 90% de desconto em leituras de cache de contexto.
3. **OpenRouter (`openrouter.ai`)**: Fallback universal para alternar entre Claude, Grok, Gemini e DeepSeek com uma única chave.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **RCA de Falhas de Despacho de Modelo**:
   - Quando um modelo econômico/local (`qwen-fast` ou `qwen-deep`) falhar em uma tarefa de implementação ou gerar loops de sintaxe, o router realiza RCA: a complexidade foi subestimada ou o contexto foi excedido?
   - O router ajusta a matriz de despacho elevando a classe da tarefa automaticamente para evitar reincidência de falha.
2. **Otimização Rumo ao One-Shot**:
   - Analise se a escolha de modelo influenciou a necessidade de interação humana. Tarefas que exigem raciocínio complexo nunca devem ser enviadas a modelos de baixa capacidade para evitar turnos de correção desperdiçados.
3. **Extrapolação de Confiabilidade de Modelos**:
   - Registre modelos com comportamento degradado ou alucinações recorrentes no ledger (`.factory/learning/learning_ledger.json`) para que nenhum outro componente do DarkFac repita a mesma alocação.

