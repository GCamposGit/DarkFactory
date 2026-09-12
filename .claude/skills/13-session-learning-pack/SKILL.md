---
name: session-learning-pack
description: Apresenta ao usuário, ao término de cada sessão ou marco arquitetural, um Learning Pack amigável e de alta densidade cognitiva. Traduz o código implementado em múltiplos níveis Feynman (pitch de 30s para clientes/executivos, defesa arquitetural Staff+ e mecânica sob o capô), âncoras mentais analógicas, escudo de defesa contra céticos e flashcards de repetição espaçada (exportáveis para Anki/Obsidian/HTML interativo). Garante a co-evolução cognitiva contínua do operador humano em paralelo à Dark Factory.
---

# 13 - Session Learning Pack & Cognitive Uplift Engine

O **Session Learning Pack** é o motor de co-evolução cognitiva e ampliação de capacidades da Dark Factory. Ele traduz arquiteturas e códigos complexos recém-construídos em sínteses didáticas de múltiplos níveis Feynman, garantindo a retenção intelectual do desenvolvedor.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Diff de Código e Commits**: Alterações introduzidas pela tarefa ou marco arquitetural.
- **Contrato de Handoff Concluído**: `WorkflowHandoff` com seus objetivos e critérios de aceitação.
- **Evento Sucessor**: Acionado após a conclusão e validação de um ticket ou sessão.

### Ações e Procedimento Executável
1. **Extração Headless dos 5 Componentes Feynman**:
   ```powershell
   python -m core.learning_pack.cli generate --title "Nome da Funcionalidade / Épico"
   ```
2. **Estruturação Cognitiva Obrigatória**:
   - **🎙️ The 30-Second Elevator Pitch**: Explicação em linguagem acessível focada em valor para clientes e executivos.
   - **🏛️ Staff+ Architectural Defense**: Trade-offs de engenharia, garantias não-funcionais e classes de falhas neutralizadas.
   - **⚓ The Mental Anchor**: Analogia intuitiva do mundo físico que fixa o conceito mentalmente.
   - **🛡️ Third-Party Defense Shield**: Respostas técnicas a perguntas espinhosas de revisores ou Tech Leads.
   - **🃏 Active Recall Flashcards**: 2 a 4 perguntas e respostas para auto-teste imediato.
3. **Apresentação e Quiz**:
   ```powershell
   # Exibir resumo no terminal
   python -m core.learning_pack.cli show latest --brief

   # Sessão interativa de auto-avaliação
   python -m core.learning_pack.cli flashcards latest -i
   ```

### Outputs Estruturados
- **Artefato Consolidado do Pack**: `.factory/learning_packs/<slug>/pack.json`.
- **Cartões de Repetição Espaçada**: Arquivo `.tsv` compatível com Anki/Obsidian.
- **Widget HTML Interativo**: Página autônoma com quiz interativo para auto-estudo.

### Portões, Política e Validação
- **Não-Bloqueio do Pipeline**: A geração do Learning Pack é um evento sucessor pós-entrega; a confirmação de leitura pelo usuário **NUNCA bloqueia** o despacho do próximo ticket ou pipeline autônomo.
- **Segurança de Conteúdo**: Nenhum segredo, credencial ou token deve ser incluído em analogias ou flashcards didáticos.
- **Qualidade Conceitual**: As explicações devem refletir o código efetivamente comitado, sem simplismos incorretos.

---

## 2. Continuous Self-Improvement & Calibração Didática

- **Calibração de Nível**: Ajuste a densidade técnica dos flashcards e da defesa arquitetural com base no feedback explícito do usuário sobre a profundidade desejada.
