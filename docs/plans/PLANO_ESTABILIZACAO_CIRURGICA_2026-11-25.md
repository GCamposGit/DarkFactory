# Plano de Estabilização Cirúrgica e Destravamento da V0 (`EC-2026-11`)

**Data:** 2026-11-25
**Épico no Roadmap:** `EC-2026-11` (`EC-01` a `EC-05` em horizonte `now`; `EC-06` em horizonte `next`, pós-V0)
**Relação com o Plano V0:** Execução tática cirúrgica para destravar e concluir as metas de [`docs/plans/PLANO_V0_2026-10-24.md`](PLANO_V0_2026-10-24.md) sem inflação de tickets.

---

## 1. Mandato Operacional do Owner

1. **Zero abertura de dezenas de meta-tickets.** É proibido abrir tickets por sintoma na frota. Os tickets meta abertos que tratam sintomas parciais (`USR-572`, `USR-554`, `USR-557`, `USR-563`, `USR-569` etc.) são absorvidos pelas **5 intervenções cirúrgicas na causa raiz (`EC-01` a `EC-05`)** deste plano.
2. **Sequência curta, determinística e auto-contida.** A sessão de coordenação (`coordenar-fabrica`) executa `EC-01` → `EC-02` → `EC-03` → `EC-04` → `EC-05` em ordem estrita, com um único PR coeso por bloco de código (`EC-02` a `EC-05`) e saldo de linhas neutro ou negativo sempre que possível.
3. **Foco em entregar produto real de cliente.** A fábrica já possui todos os componentes necessários; na 1ª tentativa do piloto `mansaodolago` (`MDL-01`, `MDL-02`, `MDL-03`), os agentes escreveram os 3 módulos completos e 71 testes passando, mas a própria maquinaria defensiva da fábrica bloqueou a entrega. O sucesso deste plano é medido pela entrega limpa de `MDL-01..03` na branch `darkfac/m1-seo` e pelo avanço real de `python -m core.line.v0_proof`.

---

## 2. Radiografia Empírica Auditada (24/10/2026 → 25/11/2026)

| Indicador Auditado | Meta V0 (24/10) | Medição Real (25/11) | Evidência de Causa Raiz |
| :--- | :--- | :--- | :--- |
| **Tamanho de `core/` + `run_ticket.py`** | `-10%` de linhas | **`145.040` LOC** (`+20.361` LOC líquidas em 278 commits) | `run_ticket.py` cresceu de `3.354` para `5.071` LOC com remendos defensivos sobrepostos |
| **Taxa de sucesso dos filhos da frota** | Fluxo contínuo estável | **6,8%** (`25` concluídos em `368` logs em `fleet_dispatcher_logs/`) | `188` abortados/incompletos, `104` `route_wait`, `51` `HARNESS_FAIL` |
| **Bloqueios em `admission.sqlite3`** | Recuperação limpa | **`18` holds ativos** em 25/11 (`663+` falhas acumuladas) | `MDL-01`, `MDL-02`, `MDL-03` em `build_evidence_mismatch`; `USR-572`, `USR-554`, `USR-557`, `USR-563` em `fix_agent_no_changes` |
| **Higiene de worktrees e disco** | Limpeza automática | **92 worktrees** + **18.000+ arquivos** em `.factory/` | `.factory/backups/trash` retém `9.204` arquivos (`1,4M` LOC duplicadas) e `.factory/tmp` retém `4.421` arquivos |
| **Prova da V0 (`core.line.v0_proof`)** | `5/5` entregas reais | **`0/5`** | Bloqueada por contradição entre `run_ticket.py` (suprime Learning Pack externo) e `v0_proof.py` (exige Learning Pack + `docs/manual/` + tabelas DAG vazias) |

---

## 3. As 6 Causas Raiz Estruturais

1. **Fragmentação de estado em 11 repositórios locais + arquivos quentes no Git:**
   - Estado dividido sem transação entre `.factory/demands/demands.json` (78 commits/mês), `control_store` (`core/demands/queue_store.py`, 195 tickets), `.factory/admission.sqlite3`, `progress.json` global por ticket, `.factory/pending_delivery/<ID>.json`, `.factory/runs/pending_deliveries.json`, `outcomes/`, `fleet_dispatcher_state.json`, `actionable_failures.json`, `owner_actions.json` e `.factory/learning/learning_ledger.json`.
   - `learning_ledger.json` rastreado no Git suja `C:\dev\DarkFac` a cada `record-turn` e quebra `git merge --ff-only` / `node_sync` (código 3 em `dokploy_redeploy.py`).
   - `core/harness/docs_sync.py` força quase todo PR do motor a tocar `docs/manual/motor.md` (49 commits) e `docs/manual/frota.md` (47 commits), gerando `rebase_conflict` sistemático entre workers.
2. **Projetos externos tratados como exceção incompleta + `BuildEvidenceMismatch` autodestrutivo:**
   - Em `run_ticket.py` (`_prepare_workspace`) e `core/line/fleet_dispatcher.py` (`resumable_delivery_worktree`), o reuso de worktree preservada só funciona para `project_id == "darkfac"` (`cwd=PROJECT_ROOT` fixo). Para projetos externos (`mansaodolago`), toda retentativa abre uma worktree nova com timestamp diferente.
   - Em `core/line/ticket_engine.py` (`resumable_build`), `progress.json` exige igualdade estrita com o caminho da worktree anterior e levanta `BuildEvidenceMismatch` fatal → 3 tentativas = `ticket_hold` permanente (`MDL-01`, `MDL-02` e `MDL-03`).
3. **Falsa distribuição de frota no Notebook + timeout classificado como bug de código (`fix_agent_no_changes`):**
   - `core/line/fleet_dispatcher.py` (`default_launch`) cria uma worktree completa `fleet-dispatch-*` **no Notebook** mesmo para `--host-id desktop` ou `grokbox`, lança `run_ticket.py` localmente (que cria uma *segunda* worktree `.worktrees/<ticket>-<ts>` no Notebook) e roda `runner.py --profile pr --local`, enfileirando todos os filhos no `suite_lock` local (até `1043s` de espera).
   - Dentro de `fleet-dispatch-*`, `PROJECT_ROOT = Path(__file__).resolve().parent` perde acesso a `C:\dev\DarkFac\.factory\usage\providers`.
   - Quando `affected.py` sofre timeout (`[STEP_TIMEOUT] affected tests`), `_gate_failure_is_timeout()` em `run_ticket.py` procura apenas `"(timeout exceeded)"`, ignora `[STEP_TIMEOUT]`, aciona `_invoke_fix_agent` sem defeito de código e tranca o ticket em `hold` como `fix_agent_no_changes` (causa exata da queda noturna de `USR-572`, `USR-554`, `USR-557` e `USR-563`).
4. **Sonda de cota do Claude Code quebrada (`539` eventos de `route_wait`):**
   - `ClaudeCodeAccountAdapter._find_claude()` (`core/usage/adapters.py`) prioriza o shim antigo `%APPDATA%\npm\claude.cmd` (`2.1.278`) sobre o binário nativo atualizado (`2.1.325+`) e falha fechado (`returncode != 0`) ao sondar com identificador literal rejeitado pelo shim antigo, tirando `opus` e `sonnet` de circulação por mais de 29h.
5. **Contradição direta entre `run_ticket.py` e `v0_proof.py` para projetos externos:**
   - `run_ticket.py` desliga o Learning Pack quando `external_project` é verdadeiro (`learning = None if external_project else ...`), enquanto `core/line/v0_proof.py` reprova qualquer ticket sem Learning Pack, exige alteração em `docs/manual/` da DarkFac e consulta jobs `integration`/`build_deploy` inexistentes no `control_store` legado.
6. **Acúmulo de código legado paralelo (`core/workflow` + `core/orchestrator`):**
   - ~25k LOC do antigo motor DAG permanecem sem execuções em produção ao lado do motor único (`run_ticket.py` + `core/line/*`), inflando complexidade e tempo de coleta de testes.

---

## 4. Plano de Execução: As 5 Intervenções Cirúrgicas (`EC-01` a `EC-05`)

### `EC-01` — Desobstrução Operacional do Host e Reconciliação de Estado
- **Tipo:** Operação direta do coordenador (sem novo PR de código do motor).
- **Ações exatas:**
  1. **Pausar o despachante em loop durante a cirurgia:** encerrar de forma limpa o processo `core.line.fleet_dispatcher --loop` ativo no Notebook e eventuais filhos órfãos (`fleet-dispatch-*`, `node_repl.exe` vazados) para cessar imediatamente a criação de novos holds e a disputa de disco/CPU.
  2. **Podar worktrees mortas e lixo de runtime:**
     - Remover as ~75 worktrees `fleet-dispatch-*` e worktrees de tickets `completed`/`cancelled` em `C:\dev\DarkFac\.worktrees\`, bem como esvaziar `.factory/backups/trash/` e `.factory/tmp/` obsoletos.
     - **Preservação obrigatória:** manter intactas as worktrees com código válido de `MDL-01` (`mdl-01-20261124T181425Z`), `MDL-02` (`mdl-02-20261124T181931Z`) e `MDL-03` (`mdl-03-20261124T183555Z`) em `C:\dev\Site_Mansaodolago\.worktrees\`, removendo apenas as worktrees vazias duplicadas de retentativa (`T185346Z`, `T194405Z`, `T185347Z`, `T194425Z`, etc.) após conferir `git status` e `git log` de cada uma.
  3. **Reconciliar `control_store` e `admission.sqlite3`:**
     - Marcar `completed` com `<sha>:merged` qualquer ticket já presente em `origin/main`.
     - Limpar em `admission.sqlite3` (`ticket_holds` e `rebase_recovery`) os holds espúrios causados pelos bugs estruturais (`MDL-01`, `MDL-02`, `MDL-03` por `build_evidence_mismatch`; tickets atingidos por `fix_agent_no_changes` em `[STEP_TIMEOUT]`).
     - Cancelar/absorver os tickets meta redundantes substituídos por `EC-02..05` (`USR-572`, `USR-554`, `USR-557`, `USR-563`) registrando referência explícita ao item `EC-*` correspondente, e encerrar/arquivar o PR `#912` (`USR-483`, segurado por criar fluxo paralelo de coordenação) para eliminar o falso stall recorrente em `core.ops.coordination`.
- **Critério de conclusão:** `git -C C:\dev\DarkFac worktree list` com menos de 10 worktrees ativas justificadas; worktrees boas de `MDL-01..03` preservadas e verificadas; zero processo `fleet_dispatcher --loop` competindo durante `EC-02..04`; zero stall falso de PR segurado em `python -m core.ops.coordination`.

---

### `EC-02` — Cirurgia nos 3 Assassinos de Entrega do Motor
- **Alvos de código:** `run_ticket.py`, `core/line/ticket_engine.py`, `core/line/fleet_dispatcher.py` (absorve integralmente `USR-572`).
- **Mudanças cirúrgicas (1 PR coeso):**
  1. **Reuso universal de worktree para qualquer projeto (`darkfac` e projetos externos):**
     - Em `run_ticket.py` (`_prepare_workspace` / `_resume_preserved_workspace`) e `core/line/fleet_dispatcher.py` (`resumable_delivery_worktree`), resolver o repositório raiz via `project_checkout(project)` em vez de restringir a `project.id == "darkfac"` ou `cwd=PROJECT_ROOT`.
     - Reaproveitar worktree preservada que contenha commits à frente da branch base **ou** árvore suja com artefatos da tentativa anterior, sem abrir nova worktree timestamped.
     - Em `core/line/ticket_engine.py` (`resumable_build`), quando `progress.json` apontar para uma worktree que não existe mais em disco ou não tem commits/diff aproveitáveis, invalidar e reiniciar `progress.json` de forma limpa e auditada em log estruturado, em vez de abortar com `BuildEvidenceMismatch`.
  2. **Fim da worktree dupla `fleet-dispatch-*` e respeito à capacidade real do host:**
     - Em `core/line/fleet_dispatcher.py` (`default_launch`), invocar `run_ticket.py` diretamente a partir de `PROJECT_ROOT` passando `DARKFAC_STATE_ROOT` explícito no ambiente (eliminando o clone efêmero `fleet-dispatch-*` por disparo e restaurando a leitura de `.factory/usage/providers`).
     - Limitar a concorrência real de subprocessos locais no Notebook e garantir que o portão de validação PR use o worker remoto saudável (`Desktop`) quando elegível, sem forçar `--local` desnecessário no Notebook.
  3. **Timeout de portão tratado como espera transitória (`infra`), nunca como `fix_agent_no_changes`:**
     - Em `run_ticket.py` (`_gate_failure_is_timeout`), reconhecer `[STEP_TIMEOUT]`, `phase=collection` e timeouts de subprocesso/pytest. Quando detectado, encerrar com código `75` (`infra` / retentativa limpa pelo despachante) **sem** chamar `_invoke_fix_agent` e **sem** incrementar rumo a `fix_agent_no_changes`.
- **Testes comportamentais obrigatórios:**
  - Retentativa de ticket de projeto externo (`project_id != "darkfac"`) após falha de entrega reutiliza a mesma worktree e preserva commits sem `BuildEvidenceMismatch`.
  - Saída do portão contendo `[STEP_TIMEOUT] affected tests` retorna código `75` sem invocar `_invoke_fix_agent`.
  - Lançamento pelo `fleet_dispatcher` propaga `DARKFAC_STATE_ROOT` sem criar diretório `fleet-dispatch-*`.
- **Critério de conclusão:** PR mesclado em `origin/main` com `[HARNESS_PASS]` e CI verde.

---

### `EC-03` — Fim dos Conflitos de Rebase por Construção e da Sujeira de Runtime no Git
- **Alvos de código:** `core/learning/` (`learning_ledger` / `cli.py`), `run_ticket.py` (fluxo de commit/PR e isolamento de `demands.json`), `.gitignore` se aplicável (respeitando `tests/test_ledger_hygiene.py` e `tests/test_gitignore_consistency.py`).
- **Mudanças cirúrgicas (1 PR coeso):**
  1. **Parar de sujar o checkout compartilhado com telemetria de turno:**
     - Gravar eventos vivos de `learning.cli record-turn` em `STATE_ROOT/learning/learning_ledger.json` (fora do índice Git ou sincronizado sem mutar o checkout em `record-turn`), eliminando a sujeira recorrente em `C:\dev\DarkFac` que obriga `git stash` manual e derruba `node_sync` com código 3 no pós-deploy.
     - Garantir que `run_ticket.py` nunca modifique `.factory/demands/demands.json` dentro da worktree de código do ticket (`control_store` é a fonte operacional da fila).
  2. **`Docs-Unchanged:` automático para correções estritamente internas:**
     - Na montagem automática de commit/entrega em `run_ticket.py`, quando o diff tocar módulos internos cobertos por `docs/manual/*.md` sem alterar flags de CLI públicas, rotas HTTP ou contratos operacionais do owner, anexar automaticamente o trailer `Docs-Unchanged: ajuste interno sem mudanca de contrato operacional` — preservando a exigência de documentação quando houver mudança real de superfície e eliminando a colisão artificial de todos os PRs em `docs/manual/motor.md` e `frota.md`.
- **Testes comportamentais obrigatórios:**
  - Executar `record-turn` deixa `git status --porcelain` completamente limpo no checkout.
  - `tests/test_demands_hygiene.py`, `tests/test_ledger_hygiene.py`, `tests/test_gitignore_consistency.py` e `core/harness/docs_sync.py` verdes.
- **Critério de conclusão:** PR mesclado em `origin/main` com checkout `C:\dev\DarkFac` permanecendo limpo após ciclos de coordenação e deploy.

---

### `EC-04` — Restauração das Sondas de Cota e Cache Curto por Execução
- **Alvos de código:** `core/usage/adapters.py`, `core/line/routing.py` (e `.factory/config/model_registry.json` apenas se necessário para o alias de sonda, respeitando `tests/test_model_id_ratchet_usr185.py`; absorve `USR-554`).
- **Mudanças cirúrgicas (1 PR coeso):**
  1. **Descoberta correta do binário Claude Code e sonda resiliente:**
     - Em `ClaudeCodeAccountAdapter._find_claude()` (`core/usage/adapters.py`), ordenar candidatos priorizando a instalação nativa mais recente (`~/.local/bin/claude.exe` ou versão semântica maior via `--version`) acima de shims antigos em `%APPDATA%\npm\claude.cmd`.
     - Passar o alias curto suportado pela CLI (`haiku`) na sonda mínima, com fallback sem `--model` caso a CLI rejeite o identificador, extraindo `rate_limit_event` sem deixar o provedor em estado `unknown` permanente.
  2. **Reuso de leitura fresca (< 5 min) entre fatias do mesmo ticket:**
     - Evitar re-executar subprocessos externos de sonda de cota a cada fatia sequencial quando já houver snapshot válido e recente (< 300 s) em `STATE_ROOT/usage/providers/`.
- **Testes comportamentais obrigatórios:**
  - Seleção do binário mais novo quando coexistem dois executáveis do Claude Code no `PATH`/disco.
  - Fallback limpo quando um modelo de sonda é rejeitado pela CLI, preservando leitura de janela de cota.
  - Validação real no host: `pick('planning', ...)` e `pick('development', ...)` enxergam cota real do Claude Code (`opus`/`sonnet` destravados).
- **Critério de conclusão:** PR mesclado em `origin/main` e snapshot real de cota do Claude atualizado em `.factory/usage/providers/`.

---

### `EC-05` — Projetos Externos de 1ª Classe, Alinhamento do `v0_proof` e Entrega de `MDL-01..03`
- **Alvos de código e entrega:** `run_ticket.py`, `core/line/v0_proof.py` (absorve `USR-557`) + entrega real de `MDL-01`, `MDL-02` e `MDL-03` em `C:\dev\Site_Mansaodolago` (branch `darkfac/m1-seo`).
- **Mudanças cirúrgicas e execução:**
  1. **Habilitar Learning Pack para projetos externos:**
     - Em `run_ticket.py`, remover o desligamento `if external_project` na geração do Learning Pack, gravando `.factory/learning_packs/<TICKET_ID>/` em `STATE_ROOT` / diretório canônico para todo ticket concluído (interno ou externo).
  2. **Alinhar `core/line/v0_proof.py` ao motor único real (`run_ticket.py`):**
     - Verificar artefatos reais emitidos pelo motor único (`merge.json` / recibo de entrega verificável + `learning_packs/<ID>` + `merge_sha` presente na `default_branch` configurada do projeto no registro, ex.: `darkfac/m1-seo` quando `requires_commercial_acceptance: true`), sem exigir commits em `docs/manual/` da DarkFac para repositórios externos e sem exigir linhas em tabelas DAG mortas.
     - Reconciliar o status de `USR-279` para refletir fielmente o placar real de `python -m core.line.v0_proof` (só `completed` quando `5/5`).
  3. **Resgatar e entregar `MDL-01`, `MDL-02` e `MDL-03`:**
     - Retomar pelo motor corrigido (`run_ticket.py`) cada uma das 3 worktrees preservadas em `C:\dev\Site_Mansaodolago\.worktrees\` (`mdl-01-20261124T181425Z`, `mdl-02-20261124T181931Z`, `mdl-03-20261124T183555Z`).
     - Rodar validação, revisão independente de outra família, geração de Learning Pack e abertura/integração de PR mirando **exclusivamente `darkfac/m1-seo`** (jamais `master` ou FTP, respeitando `requires_commercial_acceptance: true`).
- **Critério de conclusão:** `MDL-01`, `MDL-02` e `MDL-03` integrados de ponta a ponta em `GCamposGit/Site_Mansaodolago` (`darkfac/m1-seo`) com testes passando, revisão aprovada e Learning Pack gerado; `python -m core.line.v0_proof` contabilizando as entregas reais sem erro falso.

---

## 5. Pós-Estabilização (`EC-06` — Horizonte `next`, Somente Após V0 `5/5`)

> **Regra de contenção:** Não executar `EC-06` antes de concluir `EC-01` a `EC-05` e comprovar entregas estáveis.

- **Escopo de `EC-06`:**
  1. Arquivar/remover os subsistemas legados sem chamador de produção (`core/workflow/*`, caminhos mortos de `core/orchestrator/*` e módulos órfãos auditados), reduzindo `core/` em pelo menos `-10%` conforme a meta original da V0.
  2. Extrair estágios coesos de `run_ticket.py` (`5.071` LOC → `< 1.500` LOC) sem alterar contratos nem criar camadas intermediárias.

---

## 6. Protocolo para a Sessão de Coordenação (`coordenar-fabrica`)

Ao iniciar a coordenação (`Set-Location C:\dev\DarkFac; python -m core.ops.coordination` ou pedido *"assuma a fábrica"*):

1. **Não despache tickets antigos da fila antes de concluir `EC-01` e `EC-02`.** Qualquer disparo no motor ainda sem `EC-02` recriará `fleet-dispatch-*`, `build_evidence_mismatch` ou `fix_agent_no_changes`.
2. **Execute na ordem exata:** `EC-01` → `EC-02` → `EC-03` → `EC-04` → `EC-05`.
3. **Atualize o status no roadmap (`.factory/roadmap/darkfac.json`) e no `docs/operacao/HANDOFF.md`** à medida que cada bloco `EC-01..05` for concluído com evidência verificada (`commit:<ID>:<sha>`, `"verified": true`), mantendo o painel Roadmap do DarkHub 100% fiel à execução real.
4. **Religação da frota contínua:** somente após `EC-02`, `EC-03` e `EC-04` estarem em `origin/main` e sincronizados nos hosts, religue o despachante para operar `EC-05` (`MDL-01..03`) e as próximas demandas reais de produtos do owner.
