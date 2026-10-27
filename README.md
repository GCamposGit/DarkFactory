# DarkFac: Ecossistema de Skills & Fábrica Autônoma Multi-Modelo (2026 Edition)

Framework de criação autônoma de software com múltiplos harnesses de agentes (Claude Code, Codex, Grok Build, Antigravity). Os modelos são citados apenas por alias; os ids concretos vivem em `.factory/config/model_registry.json` (hoje: `opus`, `sonnet`, `haiku`, `flash`, `grok-dev`, `grok-review`, `deepseek-flash`, `gemini-pro`, entre outros).

Construído sobre o conceito de **Dark Factory** (Fábrica sem luzes / Engenharia sem supervisão no teclado), operando em **Nível 3 de Autonomia**: especificações entram como issues, passam pelo pipeline PIV, são validadas por portões determinísticos de teste e recebem *auto-merge* quando 100% dos critérios forem atingidos.

> **Escopo compartilhado:** o núcleo DarkFac e o DarkHub são versionados. Projetos locais de demonstração ficam fora do Git, dos clones e do harness compartilhado.

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
```

O fluxo completo de interoperabilidade com Antigravity, Grok e outros harnesses está em [`docs/HARNESS_INTEROP.md`](docs/HARNESS_INTEROP.md). O stack opcional de transcrição GPU está em `requirements-audio.txt`.

## Adotar ou iniciar um projeto

A DarkFac opera outros produtos por uma Project Adoption Gateway, sem misturar o roadmap da fábrica com o roadmap do produto:

```powershell
# Brownfield: cria worktree isolada e instala runtime com lock de proveniência
python -m core.adoption.cli adopt C:\dev\MeuProduto --branch codex/darkfac-adoption

# Greenfield: cria baseline Git e instala a fábrica
python -m core.adoption.cli init C:\dev\NovoProduto --name NovoProduto
```

O fluxo, a atualização idempotente, os seeds de governança e a preparação de tickets estão em [`docs/PROJECT_ADOPTION.md`](docs/PROJECT_ADOPTION.md).

---

## Arquitetura em resumo

Um motor único (`run_ticket.py`), fila no control store, despachante de frota, roteamento por cota, portão determinístico, deploy blue-green e governança protegida. Visão atual conferida contra o código: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Regras dos agentes: [`AGENTS.md`](AGENTS.md); contexto da fase: [`docs/FASE_ATUAL.md`](docs/FASE_ATUAL.md).

---

## Skills (`.agents/skills/`)

| Pasta | Propósito |
| :--- | :--- |
| `00-continuous-self-improvement` | Reavalia incidentes e falhas e transforma evidência em melhorias delimitadas de skills. |
| `01-prime-intelligence` | Mapeia e ingere a estrutura de um repositório (arquitetura, stack, convenções). |
| `02-plan-product-architecture` | Produz PRD, decisões de arquitetura e handoffs pequenos a partir da demanda. |
| `03-model-router` | Avalia tarefa, privacidade e cota para despachar modelos locais ou de nuvem. |
| `04-autonomous-piv-loop` | Executa tickets pelo ciclo Prime-Plan-Implement-Validate, fail-closed em worktrees. |
| `05-validation-harness` | Opera validação determinística (sintaxe, tipos, unidade, integração, E2E, holdout). |
| `06-adversarial-review` | Revisão adversarial com contraexemplos reproduzíveis e revisor independente. |
| `07-build-dark-factory` | Adota ou inicia projetos greenfield/brownfield pela Project Adoption Gateway. |
| `08-meta-skills-evolver` | Audita drift de regras, gasto de tokens e falhas recorrentes para evoluir skills e roteamento. |
| `09-local-audio-transcription` | Transcreve áudio localmente com faster-whisper (GPU), com separação estéreo. |
| `10-topic-deep-research` | Pesquisa aprofundada (papers, arXiv) registrada no Knowledge Ledger. |
| `11-repo-code-scout` | Minera repositórios open-source com licença permissiva e testes, para reúso. |
| `12-daily-model-benchmark` | Benchmark diário de modelos e Fronteira de Pareto custo-benefício. |
| `13-session-learning-pack` | Learning Pack pós-sessão (Feynman, âncoras mentais, flashcards). |
| `14-speculative-model-racing` | Corridas A/B/n e torneios empíricos entre modelos, com portões determinísticos. |
| `15-anti-slop-content-engine` | Geração de conteúdo com auditoria léxica anti-AI-slop e personas calibradas. |
| `16-visual-asset-studio` | Ativos visuais: banners, diagramas, mockups e ícones, com render local. |
| `17-specialized-test-subagent` | Delega execução de testes a subagentes baratos e devolve um relatório destilado. |
| `18-second-brain-knowledge` | Cliente MCP do Segundo Cérebro com citações verificáveis e fail-closed. |
| `19-run-ticket` | Ciclo completo de ticket com roteamento por cota, piso de 15% e portão oficial. |

`.agents/skills/` é a fonte; `.claude/skills/` é espelho gerado por `python scripts/sync_skills.py`.

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

### 2. Consultar Roteamento (core.line.routing.pick)
```bash
python -c "from core.line.routing import pick; print(pick('development', ['harness:claude', 'harness:codex', 'harness:grok']))"
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

### 7. Sincronizar Skills
```bash
python scripts/sync_skills.py
```

Grok e Codex seguem `AGENTS.md` e `FACTORY_RULES.md` na raiz.

---

## 📚 Documentação Completa
- [Arquitetura do Sistema](docs/ARCHITECTURE.md)
- [Manual por tema](docs/manual/)
- [Plano vigente](docs/plans/PLANO_V0_2026-10-24.md)
- [Guia e Benchmark de Modelos](docs/MODEL_SELECTION_GUIDE.md)
- [Playbook de Operação da Dark Factory](docs/DARK_FACTORY_PLAYBOOK.md)
