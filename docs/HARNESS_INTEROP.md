# Interoperabilidade entre harnesses

O repositório é deliberadamente independente do editor ou do agente. O contrato comum está em `MISSION.md`, `FACTORY_RULES.md` e `AGENTS.md`.

## Clone em outro PC

```bash
git clone <URL_DO_REPOSITORIO> DarkFac
cd DarkFac
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python core/harness/runner.py --quick
python -m pytest tests -v
```

O stack de GPU/transcrição é opcional:

```bash
python -m pip install -r requirements-audio.txt
```

## Antigravity

Abra a raiz clonada como workspace. O catálogo `.agents/skills/` é a fonte canônica das 17 skills do projeto.

## Grok

Abra a raiz clonada como workspace e injete `AGENTS.md` como instrução de projeto quando o harness oferecer essa opção. Mesmo sem carregamento automático de skills, o agente pode seguir `FACTORY_RULES.md` e executar o harness determinístico.

## Claude Code

Abra a raiz clonada como workspace. Não há `CLAUDE.md` próprio neste repositório: `AGENTS.md`, `MISSION.md` e `FACTORY_RULES.md` funcionam como o contrato equivalente. O catálogo `.claude/skills/` é o espelho sincronizado de `.agents/skills/` (via `scripts/sync_skills.py`) e é carregado automaticamente. Use o subagente declarativo `.claude/agents/test-runner.md` para delegar execução de testes conforme a skill `17-specialized-test-subagent`.

## DarkHub

```bash
python run_hub.py
```

O serviço fica em `http://127.0.0.1:8888` por padrão. Projetos locais de demonstração não fazem parte do clone compartilhado e continuam disponíveis apenas em seus próprios diretórios.

## Skills em ambientes compatíveis

- Antigravity e Codex: `.agents/skills/`.
- Claude Code: `.claude/skills/`, sincronizado pelo script `python scripts/sync_skills.py`.
- Grok e outros harnesses: use o contrato raiz e, se houver suporte a skills, aponte-o para `.agents/skills/`.

## Política de contexto seletivo e promoção de aprendizado (DF-19)

Para prevenir injeção excessiva de tokens e viés de confirmação entre diferentes harnesses:
- **Contexto Bounded (`core.orchestrator.context`)**: O orquestrador sintetiza um resumo operacional delimitado (`TaskContext`), pontuando critérios de aceitação, referências estruturadas de arquivos e marcos de progresso durável. Transcrições brutas de logs nunca são injetadas diretamente no prompt principal.
- **Promoção Fail-Closed (`core.learning.promotion`)**: Candidatos a regras (`LearningCandidate`) permanecem no status `PROPOSED` até serem validados contra uma versão específica da suíte de avaliação (`eval_version`) com pelo menos uma execução empírica comprovada (`supporting_runs`). Regras unpromoted, não avaliadas ou com divergência de versão são estritamente rejeitadas.
- **Rollback Atômico**: Regras ativas que manifestarem regressões são revertidas de forma auditável para `RETIRED`, associadas ao `rollback_ref` e justificativa formal.
