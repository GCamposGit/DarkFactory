# Agent Contract

## Antes de começar

- Leia `MISSION.md` e `FACTORY_RULES.md`.
- Preserve o escopo: o núcleo DarkFac é compartilhado.
- Use `.agents/skills/` como catálogo de skills. Grok e outros harnesses devem seguir este contrato mesmo que não carreguem skills automaticamente.

## Convenções de código

- Python 3.12+, type hints em APIs públicas e nomes `snake_case`.
- UTF-8 na entrada/saída de CLI; não imprimir emojis sem fallback seguro no Windows.
- `pathlib.Path` para caminhos e `logging` para diagnóstico.
- Pydantic v2 para contratos de dados; FastAPI apenas na camada HTTP.
- Mantenha domínio e I/O separados para permitir testes `library`, `cli` e `http`.
- Trate falhas externas (Ollama, OpenRouter, GitHub, arXiv) com fallback ou erro estruturado, sem vazar credenciais.

## Instruções ao Usuário e Configurações Manuais

- Sempre que um passo envolver configuração manual pelo usuário (dashboards, portais, integrações, arquivos de ambiente, etc.), forneça instruções passo a passo, tela por tela na versão atual da interface da plataforma.
- Forneça sugestões de conteúdo para absolutamente todos os campos que precisam ser preenchidos e seletores.
- Nunca assuma que o usuário tem experiência na configuração ou sabe o que está fazendo; o guia deve ser à prova de falhas e retrabalho.

## Intake e Portão de Ambiguidade (Gate G1)

- Toda nova demanda em linguagem natural que possua ambiguidades materiais (canais, limiares numéricos, permissões, regras de negócio não especificadas) exige pausa imediata em `WAITING_HUMAN`.
- O agente nunca deve assumir parâmetros ou iniciar código antes de executar o Grill estruturado e receber as decisões explícitas do Owner.

## Validação obrigatória

```powershell
python core/harness/runner.py --quick
```

Este é o único portão oficial. `runner.py --quick` já executa a suíte inteira
(`tests/`, ignorando `tests/test_canaletto.py`) em paralelo via
`pytest-xdist`, com verdicts cacheados por árvore de commit (reaproveita um
PASS anterior se a árvore não mudou) e enfileirados por máquina (um
`suite_lock` global impede que dois harnesses/agentes no mesmo host rodem a
suíte inteira ao mesmo tempo e disputem CPU/sqlite). Antes disso, o harness
também tenta despachar a suíte inteira para o worker primário de testes no
Desktop (`DARKFAC_TEST_WORKERS`, HF-27-11) e cai para execução local
automaticamente se o worker estiver offline/ocupado além do tempo de
espera; use `--local` para nunca despachar (detalhes em
`docs/HARNESS_INTEROP.md`, seção "Worker primário de testes (Desktop)").
Não rode `python -m pytest tests -v` separadamente como segundo portão:
isso duplicava a mesma suíte e é a causa raiz de timeouts quando múltiplos
agentes validam em paralelo no mesmo host. Detalhes de variáveis de
ambiente, locks e cache cross-host em `docs/HARNESS_INTEROP.md`.

### Loop interno

Para iteração rápida durante o desenvolvimento (antes do portão oficial),
prefira testes focados no que você tocou:

```powershell
python -m pytest tests/test_meu_modulo.py -q
```

ou, quando disponível, `python -m core.harness.affected --run` (módulo
mantido por outra frente de trabalho; calcula e roda apenas os testes
afetados pelo diff atual). Nunca use o loop interno como substituto do portão
oficial antes de declarar a tarefa concluída.

## Compatibilidade entre harnesses

- Antigravity: carrega `.agents/skills/`.
- Claude Code: carrega `.claude/skills/`, espelho sincronizado de `.agents/skills/` via `scripts/sync_skills.py`; trata este `AGENTS.md` como seu contrato equivalente a um `CLAUDE.md`.
- Grok: use a raiz clonada como workspace, leia `AGENTS.md` e `FACTORY_RULES.md` e execute os comandos acima.
- Outros agentes: `AGENTS.md` é o contrato mínimo; `docs/HARNESS_INTEROP.md` contém o fluxo de bootstrap.

## Deploy pós-merge

Depois que qualquer mudança do DarkFac chegar em `main`, rode:

```powershell
python scripts/dokploy_redeploy.py
```

Isso redeploya todos os serviços (compose + application) do projeto
`darkfac-core` no Dokploy (ambiente `production`) e espera cada um terminar
(`done`/`error`/timeout), reportando o resultado por serviço. Relate o
resultado (sucesso ou qual serviço falhou/expirou) ao Owner ou na
conclusão do ticket. Use `--list` para só listar os serviços descobertos
(nome, tipo, id, status, título do último deploy) sem disparar nada, e
`--only NOME` (repetível) para restringir a um subconjunto. Credenciais vêm
de `DOKPLOY_API_URL`/`DOKPLOY_API_KEY` (variáveis de ambiente; no Windows há
fallback automático para o registro do usuário) — nunca imprima esses
valores. `--project` é travado por um allowlist (`ALLOWED_PROJECTS` no
script, hoje só `darkfac-core`) — qualquer outro valor sai com código 2
antes de qualquer chamada HTTP, já que a permissão do Claude Code libera
`python scripts/dokploy_redeploy.py *` com qualquer argumento sem prompt.
Detalhes completos, variáveis, exit codes e o runbook de configuração do
token em `docs/HARNESS_INTEROP.md` e `docs/runbooks/dokploy_redeploy.md`.
