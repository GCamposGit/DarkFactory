---
name: anti-slop-content-engine
description: Motor headless multi-propósito de geração e filtragem de conteúdo com garantias anti-AI-slop. Calibrado por personas (LinkedIn, Blog Técnico Staff+, Proposta Comercial B2B, Release Notes, Memos), executa auditoria léxica determinística contra clichês e buzzwords, analisa a variância rítmica de sentenças para evitar cadência robótica monótona e roda um loop de auto-refinamento (Critique & Scrub Loop).
---

# 15 - Anti-AI-Slop Content Engine & Persona Stylist

O **Anti-AI-Slop Content Engine** blinda as comunicações técnicas e publicações da Dark Factory contra textos genéricos, prolixos e repletos de clichês robóticos, injetando restrições rígidas e medindo matematicamente o índice de pureza textual (`slop_score`).

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Tópico ou Texto Candidato**: Tema técnico ou rascunho de documento a ser auditado.
- **Preset de Persona**: `linkedin_post`, `technical_blog`, `commercial_proposal`, `release_notes` ou `executive_memo`.
- **Diretrizes de Estilo**: Formalidade, concisão e densidade técnica exigidas.

### Ações e Procedimento Executável
1. **Geração Inicial ou Ingestão de Rascunho**:
   - Geração local-first (Custo $0) via gerador procedural determinístico ou modelo local (Ollama).
   - Invocação via CLI:
     ```powershell
     python -m core.content.cli generate --topic "Título do Artigo" --type technical_blog --offline
     ```
2. **Linter Determinístico Anti-AI-Slop**:
   - Escaneamento contra léxico de 50+ termos banidos (*"delve"*, *"tapestry"*, *"game-changer"*, *"divisor de águas"*).
   - Análise de variância rítmica ($\sigma^2$) de sentenças para combater a cadência monótona de LLMs.
   - Banimento de aberturas vazias (*no throat-clearing*).
   - Invocação de linting:
     ```powershell
     python -m core.content.cli lint --text "Texto para auditoria..."
     ```
3. **Loop Critique & Scrub (Auto-Refinamento)**:
   - Aplique substituição determinística de muletas e clichês até atingir índice aceitável de pureza:
     ```powershell
     python -m core.content.cli scrub --text "Texto com clichês..."
     ```

### Outputs Estruturados
- **Documento Purificado**: Texto final estruturado persistido em `.factory/content/<slug>.md`.
- **Relatório de Pureza Textual**: Diagnóstico contendo `slop_score` (0 = Pristine a 100 = Toxic Slop), lista de termos purificados e índice de variância de ritmo.

### Portões, Política e Validação
- **Threshold Inviolável de Pureza**: Conteúdos com `slop_score` acima do limite tolerado pelo preset são rejeitados pelo linter.
- **Proibição de Falsos Vereditos**: Textos gerados não podem omitir métricas reais ou inventar dados técnicos para preencher formato.
- **Custo Zero por Padrão**: Toda validação opera 100% offline e localmente sem requisições externas pagas.

---

## 2. Continuous Self-Improvement & Calibração Léxica

- **Expansão de Léxico**: Quando um novo padrão de jargão artificial for identificado em revisões, adicione o termo ao léxico de restrições negativas e atualize os testes do engine.
