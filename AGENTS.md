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
- Toda ação que só o owner pode fazer deve ser registrada no mesmo turno no backlog de ações humanas (.factory/owner_actions/owner_actions.json, exibido no DarkHub) com passo a passo completo, comandos absolutos, criterio de verificacao e tickets bloqueados. E proibido pedir ao owner, apenas no chat, que descubra ou lembre o que precisa fazer.

## Intake e Portão de Ambiguidade (Gate G1)

- Toda nova demanda em linguagem natural que possua ambiguidades materiais (canais, limiares numéricos, permissões, regras de negócio não especificadas) exige pausa imediata em `WAITING_HUMAN`.
- O agente nunca deve assumir parâmetros ou iniciar código antes de executar o Grill estruturado e receber as decisões explícitas do Owner.

## Execução Canônica de Tickets e Roteamento Obrigatório (Skill 19-run-ticket)

- Toda demanda ou ticket do backlog deve ser executado seguindo a skill `19-run-ticket`.
- **Preflight Obrigatório de Cota**: Antes de gerar código ou iniciar o PIV loop, o agente/harness deve verificar a saúde de cota via `core.line.routing.pick('development')`.
- **Bloqueio de Quota Crítica no Chat**: Se a conta associada ao harness atual estiver com cota restante <= 15.0% (semanal ou janela móvel), o agente é TERMINANTEMENTE PROIBIDO de implementar código com seu próprio modelo no chat interativo. Deve recusar no chat, informar a cota restante e delegar para o harness saudavel eleito para desenvolvimento (o roteador so elege harnesses com modo `write`: Claude, Codex e Grok Build; o Antigravity e somente leitura e nunca e eleito para `development`) ou acionar o launcher headless `python C:\dev\DarkFac\run_ticket.py <TICKET_ID>`.
- **Harness de Operacao como Desenvolvedor Principal**: se o usuario opera a fabrica por um harness especifico (variavel `DARKFAC_OPERATING_HARNESS` ou autodeteccao pelo ambiente do harness pai), esse harness e eleito como desenvolvedor do estagio `development` sempre que sua cota estiver acima do piso critico de 15.0% (vence o maior headroom e a preferencia por faixa de complexidade). Se estiver critico, em cooldown, com cota desconhecida ou sem modo `write`, vale o Dynamic Headroom. A preferencia nunca relaxa o piso de 15.0%, nao altera grill/planning/review/integration (a revisao continua em outra familia que o implementador) e nao impede subagentes mais simples nem testes no desktop.
- **Roteamento de Subagentes de Desenvolvimento no Claude Code (Haiku 5.5)**: o subagente de codigo usa Sonnet (`claude-sonnet-5-5`) por padrao e Opus (`claude-opus-5-5`) em planning; o Haiku 5.5 (`claude-haiku-5-5`) so e elegivel se TODAS valerem: tipo mecanico (mechanical_edit, docs_sync, test_from_spec, config_data, boilerplate_from_template, test_run_distill, ledger_update), complexidade `low` (`medium` apenas para docs_sync e test_run_distill), no maximo 3 arquivos e 150 linhas alteradas, criterio de aceite executavel pre-definido pelo planejador, ambiguidade resolvida (Gate G1) e zero falhas anteriores do Haiku na tarefa. O Haiku e PROIBIDO em caminhos protegidos por governanca, em tags de risco (security, credentials, auth, payments, concurrency, locking, transactions, migration, data_deletion, public_contract, routing, quota, flaky_test, root_cause_debug, architecture) e nos estagios planning, grill, review e integration (nunca planeja, revisa nem resolve conflito de integracao). Escalonamento: UMA tentativa com Haiku; se o portao focado falhar ou a revisao apontar defeito de correcao, nova tentativa com Sonnet (sem iterar 3 vezes com Haiku); o Haiku nunca escala sozinho para Opus nem Fable e `forbidden_autonomous_models` continua proibido. Piso de cota de 15.0% (mesma conta Anthropic), revisao em familia diferente do implementador (o revisor nunca e Haiku) e o portao unico `python C:\dev\DarkFac\core\harness\runner.py --quick` permanecem. Implementacao: `core/line/subagent_routing.py` e `.factory/config/subagent_model_routing.json`.
- **Exceção de Override Explícito pelo Usuário**: A execução em um harness com cota <= 15.0% SÓ É PERMITIDA se o usuário exigir EXPLICITAMENTE no prompt (ex.: "forçar execução neste harness", "ignorar limite de cota", "estou ciente da cota crítica, prossiga" ou flag `--force`). Sem essa autorização textual inequívoca, o agente deve falhar fechado (*fail-closed*).

## Autonomia de Git e Sincronização Multi-Ambiente (Zero Toque Humano Pós-Grill, USR-57)

- Em desenvolvimentos internos da fábrica (`project: darkfac`), o agente é expressamente proibido de orientar o usuário a executar commits, merges ou sincronizações manuais no terminal.
- O ciclo de vida do ticket é encerrado de forma 100% autônoma pelo harness/launcher (`core.git.autonomy` / `run_ticket.py`), em TODOS os harnesses (Claude, Codex, Grok, Antigravity, locais), realizando:
  1. Commit atômico das alterações vinculadas ao ticket (`feat(...): ... [TICKET_ID]`);
  2. Push da branch de trabalho e abertura de PR (`gh pr create`);
  3. Verificação de conflito com `origin/main` e com outras branches (rebase/merge; conflito é resolvido pelo próprio agente, nunca devolvido ao usuário);
  4. Merge (`gh pr merge --squash --delete-branch`) e confirmação de que o SHA chegou em `origin/main`;
  5. Limpeza de worktrees e branches já mergeadas (local e remota);
  6. Deploy pós-merge (`scripts/dokploy_redeploy.py`) e atualização do status do ticket para `completed` em `.factory/demands/demands.json`.
- **Proibido devolver o ciclo ao usuário**: nenhum agente encerra turno pedindo comando de push, revisão/confirmação do portão, autorização de merge ou de deploy. Se uma ferramenta for negada por permissão do harness, reporte a negação exata e a regra a ajustar; não a trate como passo humano normal.
- **Defeito Encontrado = Correção Imediata ou Ticket Formal (todos os harnesses e modos)**: sempre que um agente (chat interativo, headless, subagente, worker, launcher ou linha de produção) identificar um erro, falha, bug, teste instável, regressão, inconsistência de configuração ou lacuna de processo, ele DEVE, no mesmo turno: (1) corrigir na hora, dentro do ciclo autônomo (commit, PR, merge, deploy), quando a correção for segura e pequena; ou (2) se não for possível corrigir agora (fora do escopo, exige investigação, outro harness ou acesso indisponível), abrir um ticket formal na fila de desenvolvimento (`.factory/demands/demands.json`, status `planned`, com problema, evidência e critérios de aceite) via `python C:\dev\DarkFac\run_ticket.py --create --queue-only ...` ou `core.demands`. É proibido apenas mencionar o problema na resposta, deixá-lo para 'depois' ou pedir ao usuário que o registre. O relatório final do turno deve listar cada defeito encontrado com o ID do ticket ou o commit/PR que o corrigiu.
- Apenas projetos comerciais externos com flag `requires_commercial_acceptance: true` (cliente pago em produção real; hoje nenhum) exigem autorização manual prévia para merge/deploy.

## Backups 100% Autônomos (Zero Toque Humano)

- É expressamente proibido orientar o usuário a executar rotinas manuais de backup ou restauração no terminal ou PowerShell.
- A gestão de resiliência e drills de recuperação em sandbox é 100% autônoma pela Dark Factory através do daemon agendado (`core.infra.backup_cron`), hooks pós-deploy (`scripts/dokploy_redeploy.py`), retenção assimétrica (7 dias R2 / 120 dias on-premise) e alertas críticos via Telegram em caso de anomalia.

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
