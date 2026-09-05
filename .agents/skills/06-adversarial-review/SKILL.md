---
name: adversarial-review
description: Executa auditoria técnica cruzada e adversarial de código utilizando múltiplos modelos independentes (Local gpt-review no Ollama + Nuvem DeepSeek-R1 ou Grok 4.6). Garante que o revisor pertença a uma família de modelos diferente do implementador para eliminar viés de confirmação e brechas de segurança. Use antes de aprovar ou fundir qualquer PR.
---

# Adversarial Review: Auditoria Cruzada Multi-Modelo

A revisão adversarial é a última barreira de defesa antes do código ser mesclado ao branch principal.

## A Regra da Auditoria Cruzada (Cross-Model Rule)
Quando o mesmo modelo implementa e revisa o próprio código, ele frequentemente sofre de **viés de confirmação** e ignora os mesmos pontos cegos que cometeu na codificação.
- Se a implementação foi feita por **Claude** -> Revisão por **DeepSeek-R1** ou **Gemini 3.8 Flash**.
- Se a implementação foi feita por **Gemini/Grok** -> Revisão por **Claude 3.7** ou **DeepSeek-R1**.
- Em todos os casos -> Pré-auditoria local gratuita por **`gpt-review:latest`** (Ollama).

## Fluxo de Auditoria em 2 Níveis

### Nível 1: Auditoria Rápida Local (Ollama - Custo $0)
Invoque o subagente `gpt-review:latest`:
```bash
python core/router/model_router.py call-local --model gpt-review:latest --prompt "Inspecione o diff a seguir buscando regressões óbvias, dependências ausentes e quebra de tipos: [DIFF]"
```
Se o `gpt-review` apontar falhas sintáticas ou omissões claras, retorne para o PIV loop imediatamente sem gastar tokens de nuvem.

### Nível 2: Auditoria Adversarial Profunda (Nuvem)
O auditor de fronteira avalia os seguintes eixos críticos:
1. **Verificação de Caminhos Protegidos**:
   Execute primeiro o guardrail determinístico:
   `python core/orchestrator/guard.py HEAD`
   Se houver alterações em arquivos de governança (`MISSION.md`, `FACTORY_RULES.md`), o PR é rejeitado no ato.
2. **Segurança & Injeção**:
   Sanitização de inputs, ausência de credenciais hardcoded, escape de queries e caminhos.
3. **Escopo Estrito (No Feature Bloat)**:
   O diff contém APENAS o que foi especificado no ticket? Qualquer refatoração colateral não solicitada deve ser rejeitada.
4. **Veredito Formal**:
   - `APPROVE`: Código limpo, testado e em conformidade.
   - `REJECT`: Lista clara de pendências técnicas encaminhadas para correção no loop autônomo.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Auditoria Contra o Histórico de Falhas (Ledger Check)**:
   - Antes de emitir o veredito de aprovação, o revisor adversarial deve confrontar as alterações com `.factory/learning/learning_ledger.json`.
   - O revisor verifica se o diff não está reintroduzindo falhas catalogadas nos RCAs anteriores nem violando preferências do usuário.
2. **Auditoria de Causa Raiz em Rejeições**:
   - Toda rejeição (`REJECT`) emitida pelo revisor deve ser formatada com 5-Whys estruturado (Sintoma -> Mecanismo -> Causa Raiz).
   - Isso permite que o modelo implementador corrija o problema no primeiro ciclo de volta sem necessidade de loops adicionais.
3. **Generalização de Vulnerabilidades**:
   - Se uma vulnerabilidade ou falha de tipagem for detectada no diff, o revisor assinala se o mesmo padrão de vulnerabilidade existe em arquivos vizinhos.

