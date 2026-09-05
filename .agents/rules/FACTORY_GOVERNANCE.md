# Dark Factory Governance & Inviolability Rules

Regras operacionais para funcionamento em Nível 3 de autonomia (Unattended / Lights-out Coding).

## 1. Nível de Autonomia 3 (Auto-Merge Green)

- O sistema opera em **Nível 3**:
  - Issues fatiadas e priorizadas -> Planejadas -> Implementadas -> Validadas -> Auditadas de forma adversária -> **Merge automático** quando todos os portões determinísticos emitirem `PASS`.
  - O desenvolvedor humano atua escrevendo PRDs, acompanhando releases e estabelecendo limites estratégicos.

## 2. A Lista de Arquivos Protegidos (Inviolabilidade)

Os seguintes arquivos **JAMAIS** podem ser criados, editados ou removidos por um agente autônomo sem intervenção humana explícita:
1. `MISSION.md` (Escopo e lista de *non-goals*)
2. `FACTORY_RULES.md` e `FACTORY_GOVERNANCE.md` (As regras pelas quais o agente é julgado)
3. `.agents/rules/*`
4. `core/orchestrator/guard.py` e arquivos do validation harness

Qualquer PR que toque nesses arquivos é sumariamente rejeitado pelo `core/orchestrator/guard.py`.

## 3. Linha de Independência (*The Independence Line*)

- O validador e o auditor **não devem saber como o código foi implementado**, apenas **o que foi pedido e o que o código faz agora**.
- Os testes *holdout* ficam isolados em diretório inacessível ao agente de implementação, garantindo que o modelo não "sobreajuste" (*overfit*) para passar apenas nos testes visíveis.

## 4. Auditoria Adversarial Cruzada (Cross-Model Review)

- O revisor de código em pull requests **deve obrigatoriamente pertencer a uma família de modelos diferente** da família que implementou o código.
- Se a implementação foi feita por modelo Anthropic (Claude), a revisão deve ser feita por Google (Gemini), xAI (Grok) ou DeepSeek.
- A auditoria prévia local pode ser feita pelo subagente `gpt-review:latest` no Ollama.
