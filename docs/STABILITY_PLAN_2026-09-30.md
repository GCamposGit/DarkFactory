# Plano de estabilidade e autonomia (HF-28) — 30/09/2026

Versão 1.0 · origem `user-demand` · papel: avaliação e planejamento · **sem novas features**: somente estabilidade, autonomia e eliminação definitiva de defeitos.
Complementa o [plano da linha de produção](PRODUCTION_LINE_PLAN_2026-09-22.md) (HF-27). Tickets no ledger `.factory/demands/demands.json` (tag `stability`).

## 1. Objetivo e critérios

Objetivo primordial: uma fábrica de software autônoma que desenvolve produtos para clientes finais de ponta a ponta. O operador só é consultado **na demanda inicial** e **no Grill** (intenção, negócio, segredos). Todo o resto roda sem erro, sem surpresa e sem interrupção em qualquer ambiente (Notebook, Desktop, VPS, CI Linux/Windows).

Critérios de pronto deste plano (mensuráveis):

| # | Critério | Como se prova |
| --- | --- | --- |
| S1 | `main` verde no CI em Linux e Windows, e continua verde | 10 pushes consecutivos verdes; `check-main` (USR-85) sem tickets abertos |
| S2 | Nenhum merge autônomo com CI vermelho ou pendente | teste de contrato em `core/git` + regra de branch (USR-96, owner) |
| S3 | Nenhuma suíte altera o checkout nem o `.factory` real | guarda de higiene (USR-84/88) falha com o caminho exato |
| S4 | O roteador só elege harness com a capacidade exigida | teste de contrato sobre `line_routing.json` (USR-67) |
| S5 | Falta de cota/rota não estaciona run em `waiting_human` | teste de contrato em `core/line` (USR-87) |
| S6 | Skills do caminho crítico refletem o que a linha executa | gate de drift (USR-90) verde |
| V1–V4 | Critérios do plano HF-27 §8 verificados com evidência automática | `core.line.acceptance verify` (USR-93) |

## 2. Veredito executivo

1. **A infraestrutura está saudável; o processo de desenvolvimento não.** Em 30/09 os três nós estão `converged` em `5da723e`, todos os serviços Dokploy estão `done` e o DarkHub responde `git_sha` correto. Mas o CI do `main` está **vermelho em 40 de 40 execuções** (último verde: 24/09 07:14), o roteador do ciclo interno elege um harness que não consegue escrever e o lançador local trabalha no checkout compartilhado.
2. **O CI vermelho bloqueia o piloto V1 (USR-62).** A etapa de integração da linha trata check vermelho como falha do agente e re-itera o desenvolvimento até esgotar o teto; enquanto o `darkfac` estiver vermelho na base, nenhuma demanda dele fecha o PR. Por isso a Onda 0 precede tudo.
3. **O roadmap está 100% `completed` exceto HF-27 (`validating`).** O backlog `planned` (17 tickets em 30/09) era, na prática, o registro de defeitos encontrados pela própria operação; 13 deles já tinham causa raiz parcial. Os PRs `feat(core): … [USR-N]` (#51–#73) **só registram o ticket** (+29 linhas em `demands.json`) e não implementam nada — o título engana (USR-94).
4. **A linha não consome as skills** (`core/line` não referencia skills, nem `ReadinessGate`/`WorkflowHandoff`/`EvidenceReceipt`); as skills do caminho crítico têm afirmações obsoletas ou falsas (seção 6).

## 3. Evidências (reprodutíveis)

| Evidência | Comando / fonte | Resultado em 30/09 |
| --- | --- | --- |
| Quota | `python -c "from core.line.routing import pick; …"` | `('antigravity', None)`; `anthropic` 48 %, `openai` 69 %, `google` 100 %, `xai` 0,17 % |
| CI do main | `gh run list --branch main` | 40/40 falhas; `main-validation (ubuntu-latest)` falha, `windows-latest` passa |
| Falha do CI | `gh run view 36772005908 --log-failed` | 2276 testes passam, depois `[ERROR] Candidate worktree is dirty` → `HARNESS_FAIL` |
| Reprodução local | suíte completa no Windows | `git status` limpo ⇒ a sujeira é **exclusiva do Linux** |
| Proteção de branch | `gh api …/branches/main/protection` | HTTP 404 (`main` sem proteção) |
| Nós | `python -m core.infra.node_sync verify` | Notebook, Desktop e VPS `converged` em `5da723e` |
| Deploy | `python scripts/dokploy_redeploy.py --list` | todos `done`; `darkfac-cloud` no commit #73 |
| Ledger × git | `git show --stat` dos PRs #51–#73 | apenas `demands.json` (+29 linhas) |
| Poluição de estado | `.factory/notifications/notifications.jsonl` | alertas falsos gerados por testes (`acme/repo`, canário 22/09) |
| Memória do host | suíte com `-n 4` no Notebook | `0x8007000e` (E_OUTOFMEMORY), `worker gw1 crashed` |
| Worktrees mortas | `.worktrees/` | ~14 diretórios sem `.git` (milhares de arquivos) |

## 4. Registro de defeitos: causa raiz → solução definitiva → prevenção

Regra: nenhum defeito fica só descrito. Cada linha tem correção imediata (PR deste plano) ou ticket com causa raiz e critérios de aceite.

| ID | Defeito | Causa raiz | Solução definitiva | Prevenção estrutural | Destino |
| --- | --- | --- | --- | --- | --- |
| D01 | CI do main vermelho há 6 dias | suíte suja o checkout no Linux; o runner (protegido) não lista os caminhos | guarda de higiene em `tests/conftest.py` + correção dos testes ofensores | guarda roda em toda suíte e nomeia o arquivo | **USR-84** (Onda 0) |
| D02 | merges com CI vermelho; `main` sem proteção; CD pulado | ciclo interno (`_deliver`) nunca tratou CI como portão | `core/git/ci_checks.py` compartilhado; `check-main` abre ticket | contrato: `gh pr merge` só via `ensure_green()` | **USR-85**, USR-96 |
| D03 | V1 bloqueado por CI vermelho pré-existente | integração trata todo check vermelho como culpa do diff | classificar `base_red`, reesperar e escalar | contador de iterações do agente não cresce em `base_red` | **USR-86** |
| D04 | roteador elege harness sem escrita | config lista `antigravity` primeiro; router não conhece capacidade | tabela `HARNESS_CAPABILITIES` + `pick(mode=)` | teste de contrato sobre `line_routing.json` | **USR-67** |
| D05 | agente falha sem mensagem | `run_ticket` ignora `error_kind`/exit/stderr; retry só na linha | helper único `agent_retry` | `run_ticket` e `stage_build` usam o mesmo helper | **USR-68** |
| D06 | sessões se contaminam; worktrees mortas | agente roda no checkout compartilhado; `git add -A`; remoção falha no Windows | worktree por ticket + limpeza robusta + sweep de órfãs | `run_ticket` recusa checkout compartilhado sujo | **USR-69** |
| D07 | colisão de IDs | `max(id)+1` local | alocador único com lock atômico | higiene falha com ID duplicado de título diferente | **USR-75** |
| D08 | ledger esvaziado por commit | nenhuma guarda pré-commit | bloqueio de diff que esvazia `.factory/*.json` | teste proíbe `rev:path` em shell | **USR-76** |
| D09 | run parado em `waiting_human` por falta de cota | condição temporal modelada como espera humana | `retry` com `not_before` no próximo reset | contrato: sem `waiting_human` por rota | **USR-87** |
| D10 | testes escrevem no `.factory` real | sem raiz de estado injetável | `core.paths.state_root()` + fixture autouse | guarda falha se o `.factory` real mudar | **USR-88** |
| D11 | gate intermitente / fallback local | D01 + D12 + D10 | fechar as três causas (o motivo do fallback já é impresso; persisti-lo no veredito é patch humano opcional) | guarda de higiene + raiz de estado injetável | **USR-70** (guarda-chuva) |
| D12 | workers xdist morrem no Notebook | `-n auto` sem olhar memória; WMI no start | cap por memória fora de `core/harness` | diagnóstico de pico por worker | **USR-95** |
| D13 | flake R12 (`STORE_UNAVAILABLE`) | timeout/lock SQLite sob carga (hipótese) | cenário determinístico | 10 execuções consecutivas | **USR-83** |
| D14 | nó desatualizado / legado / restart lento | node_sync só compara SHA e lê `/health` uma vez | classificar ahead/diverged/dirty; reparo seguro; espera pós-restart | alerta por divergência > 1 ciclo | **USR-71, 78, 81, 82** |
| D15 | `dokploy_redeploy` mudo, com Traceback e backup silenciado | stdout em buffer; sem `except` de topo; skip sem motivo; lógica de teste em produção | saída com flush, erro estruturado, motivo em todo skip | teste proíbe `pytest` em `sys.modules` no script | **USR-72** |
| D16 | ticket de deploy `completed` sem prova | portão usa fakes | critério `live`: `completed` só após convergência | teste de contrato no `complete_ticket` | **USR-73** |
| D17 | PRs de fila parecem entregas | mesmo título para dois eventos | `kind=queue|implementation` e `completed` com evidência | higiene do ledger exige evidência | **USR-94** |
| D18 | skills com afirmações falsas; `sync_skills --help` executa o sync | texto livre sem verificação | `Binding` por skill + gate de drift + `--check` | gate no portão oficial | **USR-89, 90** |
| D19 | ciclo de aprendizado aberto | ninguém escreve `LESSONS.md` | `retrospective` determinístico | teste: lição chega ao planning seguinte | **USR-91** |
| D20 | backlog interno sem consumidor | dogfood lê só o manifesto do roadmap | incluir `demands.json` (`line-ok`) | testes de gates | **USR-92** |
| D21 | V1–V4 sem evidência; canário invisível | verificação manual | `core.line.acceptance verify` + `/api/line/status` | HF-27 só sai de `validating` com evidência | **USR-93** |
| D22 | `MISSION.md` contradiz o objetivo | texto do núcleo headless antigo; arquivo protegido | texto proposto (apêndice B) | — | **USR-96** (owner) |
| D23 | USR-77 obsoleto | correção já está em `main` (`mock://`) | **encerrado nesta PR com evidência** | — | feito |
| D24 | USR-78 afirmava "semanas" sem medir | texto sem evidência | **corrigido nesta PR**; medir idade do commit no USR-82 | campo `commit_date` | feito / USR-82 |
| D25 | testes fixam contagens, IDs e hashes de arquivos vivos (roadmap, claims) | arquivos vivos tratados como fixtures seladas; editar o manifesto exige mexer em 3 arquivos | invariantes no lugar de igualdade; claim aponta para blob imutável | regra: hash literal de `.factory/` exige allowlist | **USR-97** |

## 5. Triagem do backlog existente (17 tickets `planned` em 30/09)

| Decisão | Tickets | Motivo |
| --- | --- | --- |
| **Fechar** | USR-77 | já resolvido em `main` |
| **Manter e endurecer (causa raiz + solução definitiva + ordem)** | USR-67, 68, 69, 70, 71, 72, 73, 75, 76, 78, 81, 82, 83 | caminho crítico de estabilidade/autonomia |
| **Piloto V1, sem implementar em chat** | USR-62 | é o veículo de verificação do V1; só entra na linha depois de USR-84/85/86 |
| **Rebaixar** | USR-79 | problema do app Codex desktop; o caminho headless não é afetado |
| **Congelar (feature/segurança)** | USR-59, USR-61 | fora do escopo "sem novas features" |
| **Bloqueado por entrada humana inevitável** | JRV-01 | exige `GROQ_API_KEY` (portal de terceiros) |

Novos tickets (14): USR-84 a USR-97 (tabela da seção 4).

## 6. Avaliação das skills frente à esteira

Método: extração determinística dos caminhos, módulos, flags e afirmações de cada `SKILL.md`, verificação contra o código e contagem de importadores de cada pacote de apoio fora do próprio pacote.

Achado estrutural: **a linha (`core/line`) só consome `router` (parcial), `harness`, `usage` e `execution`**. Nenhuma skill é carregada pela linha; o contrato da linha vive em `core/line/prompts/*.md` e nos estágios. As skills são contratos para agentes interativos (Antigravity, Claude Code, Codex, Grok).

| Skill | Papel | Consumidor real | Estado | Ação |
| --- | --- | --- | --- | --- |
| 00 continuous-self-improvement | aprendizado | `core.learning`; a linha não o alimenta | ciclo aberto | USR-91, USR-89 |
| 01 prime-intelligence | ingestão de contexto | equivalente em `workspace` + prompt de planning | parcial | binding (USR-89) |
| 02 plan-product-architecture | PRD/handoff | `stage_planning` (SPEC + tickets) sem `WorkflowHandoff` | **divergente** | binding e N/A explícito |
| 03 model-router | roteamento | `core/line/routing.py` + `line_routing.json` | **matriz de modelos obsoleta** | USR-67, 89 |
| 04 autonomous-piv-loop | PIV | `stage_build` (validate loop) | **divergente** (estados HF; 5 × 3 iterações) | USR-89 |
| 05 validation-harness | portão | `runner.py`, `scripts/line_validate.py` | íntegra | nota de árvore suja/snapshot |
| 06 adversarial-review | revisão | `stage_review` (família diferente) | conceito ok; recibos não emitidos | USR-89 |
| 07 build-dark-factory | adoção | `core.adoption` (greenfield V4) | íntegra | allowlist no gate |
| 08 meta-skills-evolver | evolução | `core.evolution` | cita `state.json` (superado) | USR-89 |
| 09, 10, 11, 12, 13, 14, 15, 16, 18 | auxiliares | Telegram/hub/jobs diários/MCP | fora do caminho crítico | nota "auxiliar" |
| 17 specialized-test-subagent | testes destilados | agente `test-runner`, `remote_worker` | íntegra | — |
| 19 run-ticket | lançador local | `run_ticket.py` (ciclo interno, **não** a linha) | **afirmações falsas** (Antigravity fixo; `--allow-critical-quota` inexistente; 5 iterações) | USR-67, 89 |

Decisão: **não** acoplar a linha às skills (evita segunda fonte de verdade). Em vez disso, cada skill do caminho crítico declara o *binding* com o estágio/módulo real, deixa de duplicar configuração e é validada por teste (USR-89 e USR-90).

## 7. Plano de execução

Princípios: ondas pequenas, propriedade de arquivos disjunta por frente, um PR por frente, portão oficial antes do merge, nenhuma edição em arquivos protegidos por `guard.py` (`core/harness/*`, `harness.config.json`, `.github/workflows/*`, `AGENTS.md`, `MISSION.md`, `FACTORY_RULES.md`, `.agents/rules/*`).

| Onda | Frente | Tickets | Arquivos principais | Depende de |
| --- | --- | --- | --- | --- |
| **0** | A. CI verde | USR-84 | `tests/conftest.py`, testes ofensores | — |
| **0** | B. Roteador e lançador | USR-67, 68 | `core/line/agent_cli.py`, `routing.py`, `run_ticket.py` | — |
| **0** | C. Portão de CI | USR-85 | `core/git/ci_checks.py`, `autonomy.py` | USR-84 |
| 1 | D. Ciclo interno robusto | USR-69, 75, 76, 94 | `core/git/*`, `run_ticket.py`, `core/demands/` | USR-68 |
| 1 | E. Linha autônoma | USR-86, 87 | `stage_integration.py`, `stage_build.py`, `routing.py` | USR-85 |
| 2 | F. Hermeticidade | USR-88, 97, 83, 95, 70 | `core/paths.py`, `tests/`, `pytest.ini` | USR-84 |
| 2 | G. Skills (gate) | USR-90 | `tests/test_skills_drift.py`, `scripts/sync_skills.py` | — |
| 3 | H. Nós e deploy | USR-71, 81, 78, 82, 72, 73 | `core/infra/node_sync.py`, `scripts/dokploy_redeploy.py` | — |
| 3 | I. Skills (conteúdo) | USR-89 | `.agents/skills/*`, `.claude/skills/*` | USR-90 |
| 4 | J. Autonomia contínua | USR-91, 92, 93 | `core/line/*`, `hub/*` | V1 |
| Owner | K. Ações humanas | USR-96 | GitHub, `MISSION.md`, patches | USR-84 |

Executor: implementação por subagente `sonnet` em worktree isolada (passo 0: `git fetch` e verificação da base); a sessão principal planeja, revisa e integra. Antes de cada onda, preflight de cota (`core.line.routing.pick`). As frentes da mesma onda rodam em paralelo no máximo 2 por vez no Notebook (pressão de memória, USR-95).

Fluxo de entrega por frente: worktree → testes focais → commit → portão oficial (`runner.py --quick`, despachado ao Desktop) → PR → CI verde → merge (squash) → limpeza → deploy. Sem pedir nada ao owner; se o merge for negado por permissão, relatar a negação exata (ver D-perm abaixo).

### D-perm — limite de permissão observado

A sessão interativa pode ter `gh pr merge` bloqueado pelo classificador do Claude Code (`autoMode.soft_deny` em `~/.claude/settings.json`, verificado em 29–30/09). Isso não é passo humano rotineiro: é uma regra de permissão que o agente não pode editar. Se ocorrer, o PR fica aberto com CI verde e o relatório cita a regra exata a ajustar (USR-96 item 4). A linha na VPS faz merge por conta própria.

## 8. Entradas humanas inevitáveis (lista fechada)

Além da lista do plano HF-27 §7 (tokens Claude/Codex na VPS, SSH, Tailscale, PAT, credenciais de terceiros, billing, respostas de negócio do Grill), este plano adiciona somente o **USR-96**: regra de proteção do `main`, atualização do `MISSION.md` e commit humano do patch opcional de `core/harness/*`. Nada mais depende do owner.

## Apêndice A — Guia passo a passo: proteção do `main` (fazer só depois do USR-84 verde)

Pré-requisito: o último push em `main` com `DarkFac CI` verde (Actions → DarkFac CI → execução mais recente com os dois jobs `main-validation` verdes). Sem isso a regra trava todos os merges.

1. Abra `https://github.com/GCamposGit/DarkFactory` e entre com a conta dona do repositório.
2. Clique em **Settings** (aba no topo, à direita de *Insights*).
3. Menu lateral esquerdo → **Rules** → **Rulesets** → botão verde **New ruleset** → **New branch ruleset**.
4. Preencha:
   - **Ruleset Name**: `main-requer-ci-verde`
   - **Enforcement status**: `Active`
   - **Bypass list**: deixe vazia (a fábrica só mergeia depois do CI verde, USR-85).
5. Em **Target branches** clique **Add target** → **Include default branch**.
6. Em **Rules** marque:
   - ☑ **Restrict deletions**
   - ☑ **Block force pushes**
   - ☑ **Require a pull request before merging** → *Required approvals*: `0`; desmarque todas as opções filhas (*Dismiss stale…*, *Require review from Code Owners* etc.).
   - ☑ **Require status checks to pass** → deixe **Require branches to be up to date before merging** **desmarcado** → clique **Add checks** e adicione exatamente: `pr-validation (ubuntu-latest)`, `pr-validation (windows-latest)` e `trusted-pr-policy`.
7. Clique em **Create**.
8. Verificação: abra qualquer PR aberto pela fábrica; o botão de merge deve ficar bloqueado até os três checks ficarem verdes. Se um merge legítimo travar, volte em **Rulesets → main-requer-ci-verde** e confira os nomes dos checks (devem coincidir letra por letra com os jobs do workflow).

## Apêndice B — Texto proposto para `MISSION.md` (arquivo protegido: commit humano)

```markdown
# DarkFac Mission

## Objetivo

Uma fábrica de software autônoma: recebe uma demanda em linguagem natural, esclarece com o operador apenas o que não consegue resolver sozinha (Grill) e entrega o produto ponta a ponta para o cliente final (código, testes, revisão independente, PR, CI, merge, deploy e smoke), sem intervenção humana fora da demanda inicial e do Grill. O mesmo núcleo evolui a própria fábrica (dogfood).

## Princípios

- Headless, determinístico e portátil (Windows e Linux); um clone limpo roda a validação e descobre as skills sem credenciais privadas.
- Nenhum sucesso sem efeito verificável (SHA, PR, deploy, smoke); o CI do GitHub é a verdade de `main`.
- Entradas humanas só para intenção, negócio, segredos/contas e aceite comercial.

## Escopo versionado

`core/`, `hub/`, `.agents/skills/` e espelho `.claude/skills/`, `tests/`, `docs/`, scripts de bootstrap e CI.

## Fora do escopo compartilhado

Chaves, tokens, dados pessoais, configurações específicas de máquina, pesos de modelos e artefatos regeneráveis.

## Critério de sucesso

Uma demanda enviada pelo Telegram ou DarkHub vira PR com testes, merge, deploy e smoke verde com relatório no Telegram; o canário diário passa; o `main` permanece verde em Linux e Windows.
```
