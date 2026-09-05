# DarkFac: Ecossistema de Skills & Fábrica Autônoma Multi-Modelo (2026 Edition)

Um framework completo para criação autônoma de software utilizando múltiplos agentes de codificação (**Antigravity com Gemini 3.8 Flash**, **Grok 4.6**, **Claude 3.7 Sonnet**, **DeepSeek-R1 / V4 Pro**, **Qwen3** e **Cluster Local Ollama**).

Construído sobre o conceito de **Dark Factory** (Fábrica sem luzes / Engenharia sem supervisão no teclado), operando em **Nível 3 de Autonomia**: especificações entram como issues, passam pelo pipeline PIV, são validadas por portões determinísticos de teste e recebem *auto-merge* quando 100% dos critérios forem atingidos.

> **Escopo compartilhado:** o núcleo DarkFac e o DarkHub são versionados. O Canaletto é um experimento local e fica deliberadamente fora do Git, dos clones e do harness compartilhado.

## 🚀 Primeiros passos após um clone

```bash
git clone <URL_DO_REPOSITORIO> DarkFac
cd DarkFac
python -m venv .venv
# Windows PowerShell: .\\.venv\\Scripts\\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python core/harness/runner.py --quick
python -m pytest tests -v --ignore=tests/test_canaletto.py
```

O fluxo completo de interoperabilidade com Antigravity, Grok e outros harnesses está em [`docs/HARNESS_INTEROP.md`](docs/HARNESS_INTEROP.md). O stack opcional de transcrição GPU está em `requirements-audio.txt`.

---

## 🚀 Destaques da Arquitetura

1. **Pipeline Híbrido (Local-First + Nuvem)**:
   - **Local (Ollama `localhost:11434`)**: Execução de custo $0 com `qwen-fast` (tarefas rápidas, boilerplate, schemas), `qwen-deep` (lógica local estruturada) e `gpt-review` (auditor independente de qualidade local).
   - **Nuvem de Alto Rendimento**: `Gemini 3.8 Flash` como orquestrador nativo do Antigravity (1M-2M tokens de contexto), `Grok 4.6` para pesquisas dinâmicas e resolução ágil em poucos turnos, e `Claude 3.7 Sonnet` / `DeepSeek-R1` para planejamento arquitetural profundo.
2. **Portões Determinísticos Invioláveis**:
   - Validação executada via código (`core/harness/runner.py`) e verificador de marcadores (`core/harness/markers.py`). Nenhum prompt de LLM pode aprovar um PR por conversa.
3. **Auditoria Adversarial Cruzada**:
   - Todo PR é revisado por um modelo de família diferente daquele que implementou o código, eliminando viés de confirmação.
4. **Governança Inalterável**:
   - Guardrail determinístico (`core/orchestrator/guard.py`) que bloqueia alterações não autorizadas em arquivos de governança (`MISSION.md`, `FACTORY_RULES.md`).
5. **Compatibilidade Multi-Agente**:
   - Funciona nativamente no **Antigravity** (`.agents/skills/`), **Claude Code** (`.claude/skills/`), **Cursor** e CLI.

---

## 📦 As 17 Skills do Ecossistema

| Pacote | Nome | Propósito |
| :--- | :--- | :--- |
| **00** | `continuous-self-improvement` | **Loop mestre prioritário**: checkpoint no 2º prompt, RCA 5-Whys, aprendizado ativo e meta One-Shot. |
| **01** | `prime-intelligence` | Ingestão e mapeamento massivo de codebases sem queimar tokens. |
| **02** | `plan-product-architecture` | Entrevista PRD, definição de *non-goals* e arquitetura headless. |
| **03** | `model-router` | Roteamento dinâmico de modelos e otimização de custo/performance. |
| **04** | `autonomous-piv-loop` | Loop Prime-Plan-Implement-Validate com isolamento de contexto fresco. |
| **05** | `validation-harness` | Escada de validação em 5 níveis e contrato de marcadores. |
| **06** | `adversarial-review` | Auditoria cruzada multi-modelo (Ollama local + nuvem independente). |
| **07** | `build-dark-factory` | Instalador da fábrica autônoma completa (Níveis 0 a 5). |
| **08** | `meta-skills-evolver` | Auto-evolução de regras, síntese de novas skills e detecção de drift. |
| **09** | `local-audio-transcription` | Transcrição local na GPU com faster-whisper e separação estéreo. |
| **10** | `topic-deep-research` | Fundamentação teórica, papers científicos (arXiv) e ledger de citações. |
| **11** | `repo-code-scout` | Mineração de repositórios e reúso de componentes open-source testados. |
| **12** | `daily-model-benchmark` | Benchmarking diário e Fronteira de Pareto de custo-benefício de LLMs. |
| **13** | `session-learning-pack` | **Cognitive Uplift**: Síntese executiva pós-sessão (Feynman 3 níveis, âncoras mentais, escudo de defesa, Anki e HTML interativo). |
| **14** | `speculative-model-racing` | Corridas especulativas A/B/n em cascata e torneios empíricos com os Top 3 modelos. |
| **15** | `anti-slop-content-engine` | Geração multi-propósito com linter anti-AI-slop, auditoria de cadência rítmica e personas calibradas. |
| **16** | `visual-asset-studio` | Ateliê visual para banners sociais, diagramas de arquitetura, mockups de UI e ícones com acoplamento a textos. |


---

## 🛠️ Como Usar

### 1. Pesquisa Autônoma & Mineração de Código (Knowledge Ledger)
```bash
# Classificação automática de intenção (Conceito vs Reúso de Código)
python core/research/cli.py classify "Procurar componentes prontos com testes para JWT"

# Pesquisa profunda de papers científicos com geração de Insights
python core/research/cli.py auto "Raft consensus log compaction" --limit 5

# Mineração de código no GitHub com verificação de licença permissiva e testes
python core/research/cli.py scout "faster-whisper voice activity detection" --language python --permissive-only

# Listar histórico de pesquisas auditáveis
python core/research/cli.py list
```

### 2. Consultar Recomendação de Modelo
```bash
python core/router/model_router.py recommend --task-type code_scout
```

### 3. Rodar Validação Determinística
```bash
# Validação rápida durante a implementação de uma tarefa
python core/harness/runner.py --quick

# Validação completa com checagem de marcadores
python core/harness/runner.py | python core/harness/markers.py
```

### 4. Verificar Proteção de Governança
```bash
python core/orchestrator/guard.py HEAD
```


### 5. Geração de Conteúdo Anti-AI-Slop & Auditoria Léxica
```bash
# Gerar post de LinkedIn ou artigo técnico com blindagem anti-slop
python -m core.content.cli generate --topic "Deterministic Test Harness" --type linkedin_post --offline

# Auditar texto ou arquivo em busca de clichês de IA e cadência monótona
python -m core.content.cli lint --text "In today's fast-paced world, delve into the tapestry of AI."

# Limpar e desbastar clichês de um texto automaticamente
python -m core.content.cli scrub --text "We should delve into the system and unleash a breakthrough."
```

### 6. Ateliê Visual de Ativos & Criação de Imagens
```bash
# Gerar banner social em alta definição ($0 custo, render local)
python -m core.visual.cli create --title "DarkFac Autonomous Engine" --type social_banner --theme modern_minimalist_dark --offline

# Gerar diagrama de arquitetura de microsserviços
python -m core.visual.cli diagram --title "Distributed Agent Event Bus"

# Ilustrar automaticamente um artigo ou post (acoplamento com Skill 15)
python -m core.visual.cli illustrate --text "Immutable write-ahead log for deterministic recovery" --type blog_hero --offline
```

### 7. Sincronizar Skills para outros ambientes
```bash
python scripts/sync_skills.py
```

`.agents/skills/` é o catálogo canônico para Antigravity e agentes compatíveis; `.claude/skills/` é o espelho para Claude Code. Grok deve seguir `AGENTS.md` e `FACTORY_RULES.md` na raiz.

---

## 📚 Documentação Completa
- [Arquitetura do Sistema](docs/ARCHITECTURE.md)
- [Guia e Benchmark de Modelos](docs/MODEL_SELECTION_GUIDE.md)
- [Playbook de Operação da Dark Factory](docs/DARK_FACTORY_PLAYBOOK.md)
