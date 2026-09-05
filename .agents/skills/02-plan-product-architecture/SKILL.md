---
name: plan-product-architecture
description: Converte uma ideia ou requisito bruto em um PRD formal com lista inegociável de non-goals, desenho de arquitetura headless (separação rígida entre lógica e apresentação para testes E2E) e fatiamento em tickets executáveis em uma única passada. Use no início de novos projetos ou ao planejar novos módulos/épicos.
---

# Plan Product Architecture: PRD, Arquitetura & Fatiamento

Esta skill garante que o escopo seja estritamente delimitado e que o software seja concebido de forma observável e testável antes de qualquer linha de código ser escrita.

## Modelo Recomendado
- **Primário**: `claude-3.7-sonnet` (Extended Thinking) ou `deepseek-r1` (via SiliconFlow) — modelos líderes em raciocínio causal e prevenção de dependências cíclicas.
- **Local Fallback**: `qwen-deep:latest` (Ollama) — alta capacidade lógica local com 20 threads e contexto de 16k.

## Fases do Planejamento

### Fase 1: O PRD Focado em Problema & Não-Metas
Um PRD autônomo deve responder impreterivelmente:
1. **O Problema**: Qual dor real do usuário está sendo resolvida?
2. **A Jornada Feliz (The Core Journey)**: Descreva em passos observáveis a ação mais valiosa do usuário do início ao fim.
3. **Non-Goals (Fora de Escopo Para Sempre)**:
   > A lista mais importante do projeto. Se não houver uma lista explícita de *non-goals*, o agente aceitará qualquer solicitação plausível, inflando o escopo indefinidamente.
4. **Restrição de Alcance (Reachability)**:
   A lógica de negócios **deve** poder ser exercida via terminal (CLI), chamadas de biblioteca ou endpoints HTTP locais sem depender de renderização de tela gráfica (GUI).

### Fase 2: Arquitetura Técnica & Decisões Imutáveis
Defina em `ARCHITECTURE.md`:
- **Driver de Teste**: `http`, `cli` ou `library`.
- **Forma dos Dados**: Schemas de entrada, saída e persistência.
- **Limites de Segurança**: O que pode ser automatizado vs o que requer aprovação manual (ex: pagamentos, credenciais raiz).

### Fase 3: Fatiamento em Tickets Cirúrgicos (Slicing)
Fatie o épico em tarefas que possam ser executadas em uma passada (*one-pass-ready*):
- Cada ticket deve ter um escopo máximo de 1 a 4 arquivos.
- Cada ticket deve conter o comando exato de validação (`VALIDATE_CMD`) que deve falhar antes e passar depois da implementação.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Planejamento One-Shot sem Atrito Interativo**:
   - Evite perguntas investigatórias redundantes. Deduza premissas técnicas a partir do perfil registrado em `.factory/learning/learning_ledger.json`.
   - Adote convenções de projeto consolidadas como defaults inteligentes e apresente a arquitetura pronta para execução.
2. **Aprendizado com Feedbacks e Correções de Escopo**:
   - Se o usuário precisar intervir para restringir escopo ou adicionar um *non-goal* que foi ignorado, registre isso como falha de planejamento (`python core/learning/cli.py record-turn --followup`).
   - Adicione imediatamente a regra de inferência correspondente na lista de preferências do usuário.
3. **RCA de Tickets Bloqueantes ou Subestimados**:
   - Quando um ticket fatiado pelo arquiteto falhar durante a execução por escopo amplo demais (>4 arquivos) ou testes ambíguos, execute RCA e recalibre as regras de fatiamento para que futuras tarefas sejam estritamente atômicas.

