---
name: anti-slop-content-engine
description: Motor headless multi-propósito de geração e filtragem de conteúdo com garantias anti-AI-slop. Calibrado por personas (LinkedIn, Blog Técnico Staff+, Proposta Comercial B2B, Release Notes, Memos), executa auditoria léxica determinística contra clichês e buzzwords, analisa a variância rítmica de sentenças para evitar cadência robótica monótona e roda um loop de auto-refinamento (Critique & Scrub Loop).
---

# 15 - Anti-AI-Slop Content Engine & Persona Stylist

O **Anti-AI-Slop Content Engine** é a blindagem contra conteúdo genérico, prolixo e formulaico produzido por modelos de linguagem.
Conforme as empresas são inundadas por postagens robóticas recheadas de jargões vazios (*"delve into the tapestry of AI"*, *"in today's fast-paced world"*, *"game-changer"*), este motor atua como um editor executivo implacável:
Ele injeta restrições negativas rígidas na geração, mede matematicamente o índice de impureza (`slop_score` de 0 a 100) e executa passes de lapidação para garantir voz autêntica, alta densidade de sinal e ritmo dinâmico.

---

## 🏛️ Arquitetura do Critique & Polish Loop

```text
       ┌─────────────────────────────────────────────────────────────┐
       │             INPUT CONTEXT & PERSONA PRESET                  │
       │  - Formatos: LinkedIn, Blog Técnico, Proposta, Changelog    │
       │  - Parâmetros: Formalidade, Concisão, Densidade Técnica     │
       │  - Diretivas Negativas Rígidas (Negative Slop Constraints)   │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
       ┌──────────────────────────────▼──────────────────────────────┐
       │         GERAÇÃO INICIAL (Local-First / Multi-Tier)          │
       │  - Tier 0 ($0): Motor Procedural Estruturado Determinístico │
       │  - Tier 1 ($0): Ollama Local (qwen-code-deep / qwen-fast)   │
       │  - Tier 2: OpenRouter Cloud (Claude 3.7 Sonnet / Gemini)    │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
       ┌──────────────────────────────▼──────────────────────────────┐
       │            LINTER DETERMINÍSTICO ANTI-AI-SLOP               │
       │  1. Scanner Léxico: Léxico de 50+ termos tóxicos (EN/PT)   │
       │  2. Análise de Cadência: Variância de tamanho de sentenças  │
       │  3. Anti-Monotonia: Punição a ritmos robóticos uniformes    │
       │  4. Cálculo do Slop Score: 0 (Pristine) a 100 (Toxic Slop) │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
                         [Slop Score > Limiar?]
                       ┌──────────────┴──────────────┐
                    Sim│                             │Não
       ┌──────────────▼──────────────┐               │
       │  CRITIQUE & SCRUB REWRITE   │               │
       │  - Substituição Determinística              │
       │  - Eliminação de Muletas    │               │
       │  - Re-avaliação do Score    │               │
       └──────────────┬──────────────┘               │
                      └──────────────┬───────────────┘
                                     │
       ┌─────────────────────────────▼───────────────────────────────┐
       │             CONTEÚDO FINAL PURIFICADO & SALVO               │
       │  - Persistido em .factory/content/                          │
       │  - Relatório completo de pureza e correções aplicadas       │
       └─────────────────────────────────────────────────────────────┘
```

---

## 🎯 Os 5 Pilares de Combate ao AI-Slop

1. **🚫 Banimento Léxico Inviolável**:
   - Elimina clichês universais de LLMs: *"delve"*, *"tapestry"*, *"game-changer"*, *"unleash"*, *"revolutionize"*, *"beacon of"*, *"plethora"*, *"paradigm shift"*, *"mergulho profundo"*, *"tapeçaria"*, *"divisor de águas"*.
2. **📉 Punição à Cadência Monótona (Burstiness)**:
   - Textos humanos têm alta variância de tamanho de frase: uma sentença de 4 palavras ao lado de uma de 22 palavras. LLMs tendem a produzir sentenças de comprimento uniforme (~18 a 24 palavras) de forma repetida. O linter calcula a variância ($\sigma^2$) e pune cadências monótonas.
3. **⚡ Aberturas Diretas (No Throat-Clearing)**:
   - Proíbe aberturas corporativas vazias (*"No mundo acelerado de hoje..."*, *"In today's fast-paced digital world..."*). A primeira linha deve ser um gancho de quebra de padrão ou uma métrica concreta.
4. **🏢 Formatos e Personas Especializadas**:
   - **LinkedIn Post**: Gancho forte, parágrafos de uma frase, sem hashtags inúteis, aprendizado de engenharia concreto.
   - **Technical Blog**: Tom de Staff Engineer, código executável, trade-offs, análise de falhas, métricas reais.
   - **Commercial Proposal**: Foco em custo de inação, entregáveis pontuais, matriz de ROI e mitigação de risco.
   - **Release Notes**: Organizado em Highlights, Features, Fixes e Breaking Changes.
   - **Executive Memo**: TL;DR de 3 tópicos, trade-offs e deadline claro de decisão.
5. **💰 Local-First & Custo Zero ($0)**:
   - Opera 100% offline via gerador procedural determinístico e Ollama local, escalando sob demanda para Claude 3.7 Sonnet ou Gemini 3.8 Flash quando máxima expressividade for requerida.

---

## 🛠️ Comandos da CLI (`core/content/cli.py`)

### 1. Gerar Conteúdo Anti-Slop
```powershell
python -m core.content.cli generate \
  --topic "Deterministic Test Harness in Level 3 Software Factory" \
  --type linkedin_post \
  --offline
```

### 2. Auditar Texto ou Arquivo Existente (Linter)
```powershell
python -m core.content.cli lint --text "In today's fast-paced digital world, we must delve into the tapestry of AI to unleash a game-changer paradigm shift."
```

### 3. Limpar / Desbastar Clichês Automaticamente (Scrub)
```powershell
python -m core.content.cli scrub --text "We should delve into this system and unleash a major breakthrough."
```

### 4. Listar Presets de Personas
```powershell
python -m core.content.cli presets
```

---

## 🤖 Protocolo para Agentes na Dark Factory

Sempre que a tarefa envolver redação de artigos técnicos, resumos executivos, documentação voltada ao usuário, posts de divulgação ou propostas:
1. NUNCA gere texto solto sem submetê-lo ao `AntiSlopLinter`.
2. Garanta que o `slop_score` final seja inferior a **15.0** (classificação `Pristine` ou `Clean`).
3. Se o texto for ilustrado, envie o conteúdo diretamente para a Skill 16 (`visual-asset-studio`).
