---
name: session-learning-pack
description: Apresenta ao usuário, ao término de cada sessão ou marco arquitetural, um Learning Pack amigável e de alta densidade cognitiva. Traduz o código implementado em múltiplos níveis Feynman (pitch de 30s para clientes/executivos, defesa arquitetural Staff+ e mecânica sob o capô), âncoras mentais analógicas, escudo de defesa contra céticos e flashcards de repetição espaçada (exportáveis para Anki/Obsidian/HTML interativo). Garante a co-evolução cognitiva contínua do operador humano em paralelo à Dark Factory.
---

# 13 - Session Learning Pack & Cognitive Uplift Engine

O **Session Learning Pack** é o motor de co-evolução cognitiva e ampliação de capacidades humanas da Dark Factory.
Conforme a fábrica de software avança para os Níveis 3 e 4 de autonomia, agentes constroem abstrações complexas, heurísticas matemáticas e infraestruturas distribuídas em segundos.
Para prevenir o *cognitive offloading* (atrofia intelectual do desenvolvedor) e transformar cada turno de pair programming em uma **micro-academia de alta voltagem**, este motor gera e entrega ao usuário sínteses executivas e arquiteturais de classe mundial.

---

## 🏛️ Fundamentação Científica & Pedagógica

```text
       ┌─────────────────────────────────────────────────────────────┐
       │             COGNITIVE LOAD THEORY (John Sweller)             │
       │  - Zero Extraneous Load: Sem rodeios ou jargões vazios      │
       │  - Maximize Germane Load: Construção de esquemas mentais    │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
       ┌──────────────────────────────▼──────────────────────────────┐
       │             FEYNMAN MULTI-TIER TRANSLATION                  │
       │                                                             │
       │  [Tier 1: 30-Second Cocktail/Elevator Pitch]                │
       │  → Explicação em linguagem natural para clientes/PM/CEO     │
       │                                                             │
       │  [Tier 2: Staff+ Architectural Defense]                     │
       │  → Trade-offs técnicos, invariantes e garantias não-funcionais│
       │                                                             │
       │  [Tier 3: Engine Room Mechanics]                            │
       │  → Algoritmos, estruturas de dados e protocolo de execução │
       └──────────────────────────────┬──────────────────────────────┘
                                      │
       ┌──────────────────────────────▼──────────────────────────────┐
       │             ACTIVE RECALL & DEFENSE SHIELD                  │
       │  - Mental Anchor: Metáfora viva do mundo real               │
       │  - Defense Shield: Respostas à prova de bala para o CTO     │
       │  - Flashcards Anki/Spaced Repetition: Retenção a longo prazo│
       └─────────────────────────────────────────────────────────────┘
```

---

## 🎯 Os 5 Componentes de Todo Learning Pack

1. **🎙️ The 30-Second Cocktail/Elevator Pitch**:
   - Como explicar o que foi feito em 30 segundos para um cliente, CEO ou investidor, focando em valor e impacto sem enrolação técnica.
2. **🏛️ Staff+ Architectural Defense**:
   - O racional de engenharia sênior: por que a abordagem X foi escolhida sobre Y, quais classes de falhas foram neutralizadas e quais garantias determinísticas foram estabelecidas.
3. **⚓ The Mental Anchor (Metáfora Viva)**:
   - Uma analogia vívida e intuitiva do mundo físico (ex: "como uma caixa-preta de avião" ou "como a curva de calorias de um buffet") que fixa o conceito no cérebro.
4. **🛡️ Third-Party Defense Shield (Escudo contra Céticos)**:
   - Antecipação de 1 ou 2 perguntas espinhosas que um Tech Lead, revisor de PR ou CTO faria (ex: "Por que não usar um dicionário simples?", "Como isso lida com concorrência?") com respostas técnicas irrefutáveis.
5. **🃏 Active Recall Flashcards**:
   - 2 a 4 perguntas e respostas em formato de flashcard para auto-teste imediato, com exportação nativa para Anki (`.tsv`), Markdown e HTML interativo.

---

## 🤖 Protocolo Obrigatório para Agentes (Ao Final de Cada Sessão)

Ao concluir uma tarefa, épico, correção de bug ou ao preparar a passagem de bastão de uma sessão, o agente deve:

1. **Disparar a Extração Headless**:
   ```powershell
   python -m core.learning_pack.cli generate --title "<Nome do Épico / Funcionalidade>"
   ```
2. **Apresentar o Brief Cognitivo no Chat**:
   - Incluir a saudação amigável e destacar o pitch de 30s.
   - Fornecer o Mental Anchor e o Defense Shield.
   - Apresentar os flashcards para auto-avaliação do usuário.
   - Fornecer o link clicável para o widget HTML interativo gerado em `.factory/learning_packs/`.

---

## 🛠️ Comandos da CLI (`core/learning_pack/cli.py`)

### 1. Gerar Novo Learning Pack a partir do Git Diff
```powershell
python -m core.learning_pack.cli generate --title "Implementação do State Machine Determinístico"
```

### 2. Listar Packs Anteriores
```powershell
python -m core.learning_pack.cli list
```

### 3. Exibir Pack Específico no Terminal
```powershell
python -m core.learning_pack.cli show latest
# Ou em modo compacto:
python -m core.learning_pack.cli show latest --brief
```

### 4. Sessão de Quiz Interativo no Terminal
```powershell
python -m core.learning_pack.cli flashcards latest -i
```

### 5. Exportar Baralho para o Anki
```powershell
python -m core.learning_pack.cli export-anki latest --out .factory/decks/session_anki.tsv
```

### 6. Abrir Widget HTML Standalone no Navegador
```powershell
python -m core.learning_pack.cli html latest --open
```

---

## 🌐 Endpoints REST do DarkHub

O DarkHub (`hub/backend/api.py`) expõe nativamente os seguintes endpoints:

| Método | Rota | Descrição |
| :--- | :--- | :--- |
| `GET` | `/api/learning-packs` | Lista o índice de todos os learning packs registrados |
| `GET` | `/api/learning-packs/latest` | Retorna o último pack completo em JSON |
| `GET` | `/api/learning-packs/{pack_id}` | Retorna o pack especificado por ID |
| `POST`| `/api/learning-packs/generate` | Gera um novo pack a partir do estado atual |
| `GET` | `/api/learning-packs/{pack_id}/export-anki` | Baixa o deck TSV formatado para o Anki |
| `GET` | `/api/learning-packs/{pack_id}/html` | Retorna o HTML interativo com flip cards 3D |

---

## 🏛️ Governança & Padrões

1. **Local-First & $0 Cost**: O extrator padrão roda deterministicamente via AST e análise de diffs sem custo de tokens, com enriquecimento opcional via Ollama local (`qwen-fast`/`qwen-deep`).
2. **Inviolabilidade de Artefatos**: Todos os packs são persistidos de forma permanente e auditável em `.factory/learning_packs/`.
3. **Compatibilidade Multi-Agente**: Espelhado automaticamente para `.claude/skills/13-session-learning-pack/` via `python scripts/sync_skills.py`.
