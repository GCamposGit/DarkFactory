<!-- covers: core/line/v0_proof.py, core/line/owner_intake.py, core/line/acceptance.py, core/line/pilot_secrets.py, hub/backend/api.py, hub/backend/service_line.py, hub/frontend/live.html, hub/frontend/live.js, hub/frontend/live.css -->
# Prova da V0 (USR-279)

Critério aprovado pelo owner em 24/10 (`docs/plans/PLANO_V0_2026-10-24.md`, seção 1): **5 demandas
reais seguidas** entram por Telegram, Hub ou chat e chegam em produção **sem o owner tocar**, cada
uma com learning pack; **pelo menos 1** é de um produto real (`project_id` diferente de `darkfac`);
os três canais funcionam sem erro.

`core.line.v0_proof` traduz essa regra em código: `evaluate_v0_proof` é a função pura que decide
se o critério foi cumprido; `collect_v0_proof_evidence` monta a entrada dela a partir das stores
que a linha já escreve.

## A regra (`evaluate_v0_proof`)

Função pura: sem I/O, sem relógio implícito (`now` é sempre passado pelo chamador). Recebe uma
lista de `DeliveryEvidence` em ordem cronológica (mais antiga primeiro) e devolve um
`V0ProofReport` com:

- `current_streak`: tamanho da sequência de entregas qualificadas que termina na mais recente;
- `qualified_deliveries`: essas entregas, em ordem, cada uma com `qualifies` e, quando desqualificada,
  `reasons`;
- `channels_covered` e `projects_involved`: canais e projetos presentes **nessa sequência**;
- `criterion_met`: `True` só quando a sequência tem `required_streak` (5) entregas, cobre os três
  canais (`telegram`, `hub`, `chat`) e inclui pelo menos um projeto diferente de `darkfac`;
- `missing`: frases prontas dizendo o que falta (quantas entregas, qual canal, falta de produto real);
- `slots`: a mesma sequência projetada em exatamente `required_streak` posições fixas
  (`build_v0_proof_slots`), para o painel `/live` do DarkHub (ver seção abaixo) — nunca usado pela
  própria regra para decidir `criterion_met`.

Uma entrega só qualifica quando **todos** os itens abaixo são verdadeiros; a falta de qualquer um
desqualifica a entrega e zera a sequência (a próxima entrega qualificada recomeça a contagem do 1):

| Item | Campo em `DeliveryEvidence` |
| --- | --- |
| Canal é `telegram`, `hub` ou `chat` | `channel` |
| Nenhuma ação do owner fora do grill e do aceite | `owner_actions_count == 0` |
| No máximo uma decisão do owner (o toque de aceite) | `owner_decisions_count <= 1` |
| Learning pack vinculado ao ticket | `learning_pack_linked` |
| Página de `docs/manual/` alterada no merge | `manual_page_changed` |
| SHA de merge presente | `merge_sha` |
| Smoke do deploy verde | `smoke_passed` |
| Entrega não está em andamento | `not in_progress` (uma entrega em andamento nunca conta como verde) |

O grill nunca aparece aqui: ele é respondido pelo mecanismo próprio da linha (`grill_answers` +
botão do Telegram, ver `docs/manual/fila.md`), não cria `OwnerAction`. Por isso qualquer
`OwnerAction` do tipo `action` ligada ao ticket durante o run já desqualifica, e no máximo uma
`decision` (o aceite, por exemplo aprovar um post antes de publicar) é tolerada.

## O coletor (`collect_v0_proof_evidence`)

Reaproveita as stores que a linha já escreve, sem criar uma fonte de verdade paralela:

- **Canal**: `owner_intake.submit_owner_demand` (entrada única do Telegram, Hub e chat, ver
  `docs/manual/fila.md`) grava uma tag `channel:<telegram|hub|chat>` no `UserTicket` criado a
  partir do parâmetro `source`. O coletor lê essa tag de volta via `DemandsStore.list_tickets()`.
  Um ticket sem essa tag é anterior à instrumentação (ou não passou por
  `submit_owner_demand`) e é **excluído do universo de candidatos**, não tratado como entrega
  desqualificada — não faz parte do que a V0 está medindo.
- **Run e evidência de entrega**: `core.line.owner_intake.open_line_store`, `run_state` e
  `extract_run_delivery_evidence`, localizando o run pelo `ticket_external_id` (mesmo esquema de
  `channel="owner"` + `ticket:<id>` usado por `submit_ticket_to_line`).
- **SHA de merge e smoke**: lidos diretamente dos jobs do run (`get_run_status`): o job do estágio
  `integration` guarda `[pr_url, merge_sha]`; o job do estágio `build_deploy`
  (`core.line.stage_release.ReleaseStageHandler`) guarda uma referência `smoke:<run_id>:<sha>` só
  quando o smoke pós-deploy passou.
- **Página de manual alterada**: `git show --name-only <merge_sha>` no repositório, checando se
  algum caminho começa com `docs/manual/`.
- **Learning pack**: existência de `.factory/learning_packs/<ticket_id>.json` ou `.md`
  (`core.learning_pack.delivery.build_for_delivery`, mesma convenção usada por
  `core.ops.turn_guard`).
- **Ações do owner**: `core.owner_actions.store.OwnerActionStore.list_actions()`, filtrando as que
  referenciam o ticket em `blocks`/`depends_on` e foram criadas dentro da janela do run
  (`created_at` até `completed_at`/`updated_at`).
- **Projetos**: `core.projects.registry.get_project_registry()` só para anotar (em
  `source_notes`, sem desqualificar) quando o `project_id` do ticket não está registrado.

Fonte indisponível nunca derruba o coletor: cada store é aberta dentro de um `try/except`
específico, e a falha vira um código estruturado em `V0ProofReport.degraded_sources` (por exemplo
`control_store_unavailable`) — nunca o texto bruto da exceção, que pode carregar uma credencial.

## Relatório

`save_v0_proof_report(report)` grava `V0ProofReport.model_dump_json()` em
`.factory/reports/v0-proof/<data>.json` (data extraída de `generated_at`, mesma convenção de
`core/line/canary.py`). `save_v0_proof_markdown(report)` grava o mesmo conteúdo em
`<data>.md` via `render_v0_proof_markdown` — cada linha é um fato ou uma ação da própria
fábrica, nunca uma pergunta ao owner (o que falta vem pronto em `report.missing`, já no formato de
ação, ver seção acima). As duas escritas são idempotentes: a mesma entrada produz os mesmos bytes,
e o arquivo não é regravado quando já contém esse conteúdo.

### CLI

```powershell
python -m core.line.v0_proof --report
```

Chamado sem `--report`, o comando só coleta a evidência e imprime o resumo (streak, canais,
projetos e o que falta) em UTF-8, sem gravar nada e sempre saindo `0` — útil para checar o status
rápido em qualquer terminal. Com `--report`:

- grava `.factory/reports/v0-proof/<data>.json` e `.md` (use `--out-dir` para outro diretório);
- imprime o mesmo resumo em UTF-8;
- sai `0` quando `criterion_met` é verdadeiro e `1` quando ainda falta algo — o código de saída é o
  que o portão/cron usa para decidir se a prova da V0 já fechou, sem precisar abrir o json.

### Leitura do relatório

O `.md` traz, nesta ordem: status (`CUMPRIDO`/`EM ANDAMENTO`), tamanho do streak qualificado,
canais e projetos cobertos pela sequência atual, a lista de entregas que compõem esse streak e,
quando o critério ainda não fechou, a seção "O que a fábrica ainda precisa entregar" — frases
prontas de `V0ProofReport.missing` (ex.: quantas entregas faltam, qual canal falta, falta de
produto real). Uma seção final "Fontes indisponíveis nesta leitura" lista, por código estruturado
(nunca o texto bruto da exceção), qualquer store que não pôde ser lida nessa execução
(`degraded_sources`).

### Integração com o probe de aceite (`core.line.acceptance`)

`core.line.acceptance.verify_v0` adapta `V0ProofReport` ao mesmo formato `CriterionResult` de
V1–V4 (`id`, `title`, `verified`, `evidence`, `details` com `missing`/`next_step` preenchidos
quando o critério não está cumprido). `run_acceptance_verification` inclui V0 na lista de
critérios do relatório de aceite da linha (`.factory/reports/line-acceptance/`), junto de V1-V4,
sem alterar o formato existente do json/md nem a reconciliação do manifesto de roadmap
(`reconcile_roadmap_manifest` continua olhando só para `all_verified`/`verified_count`, agora sobre
5 critérios em vez de 4).

```powershell
python -m core.line.acceptance verify
python -m core.line.acceptance status
```

## Painel no DarkHub (`/live`) e endpoint (USR-279 T3)

`GET /api/line/v0-proof` (mesmo router `/api` dos outros endpoints de leitura da esteira, sem
sessão exigida — mesmo padrão de `GET /api/line/live` e `GET /api/line/status`) devolve o
`V0ProofReport` tipado direto de `collect_v0_proof_evidence`, incluindo o novo campo `slots`:
exatamente `REQUIRED_STREAK` (5) entradas, mais antiga primeiro, cada uma com `filled`,
`ticket_id`, `project_id`, `channel`, `run_id`, `merge_sha`, `delivery_evidence` (texto com o
PR/commit, o mesmo de `extract_run_delivery_evidence`), `smoke_passed`, `learning_pack_linked` e
`manual_page_changed`. Quando a sequência qualificada tem menos de 5 entregas, os slots iniciais
vêm com `filled=False` (`build_v0_proof_slots`) — o painel mostra essas posições como "aguardando
demanda" em vez de escondê-las, para deixar claro quantas faltam.

`HubService.get_v0_proof` (`hub/backend/service_line.py`) nunca propaga exceção para a rota: se o
store da linha não abre, cai para `collect_v0_proof_evidence(store=None, ...)` (que tenta seu
próprio fallback e registra `control_store_unavailable` em `degraded_sources` se também falhar); se
a própria coleta falhar de um jeito inesperado, devolve um `V0ProofReport` vazio com
`degraded_sources=["v0_proof_collect_failed"]`. Em qualquer um desses casos o endpoint responde
`200`, nunca `500` — só o conteúdo do relatório muda.

O painel `/live` (`hub/frontend/live.html` + `live.js` + `live.css`) busca esse endpoint em um
ciclo próprio (a cada 60 s, independente do SSE da esteira) e desenha a seção "Prova da V0" na
coluna lateral: o rótulo do cabeçalho mostra `cumprido` ou `streak/5`; cada slot preenchido mostra
ticket, projeto, canal, os três selos (`smoke`, `learning pack`, `manual`) e um link para o PR/commit
quando `delivery_evidence` traz uma URL; slots vazios aparecem como um cartão tracejado
"aguardando demanda", sem poluir a tela quando a prova ainda não começou. Quando `missing` ou
`degraded_sources` não está vazio, uma linha de nota aparece abaixo dos slots com o que falta e,
se for o caso, quais fontes estão indisponíveis nessa leitura.

Essa tolerância é deliberada e local a este painel (nunca `500`, degrada o conteúdo). As rotas
`GET /api/demands/tickets[/{id}]`, no mesmo `hub/backend/api.py`, seguem a política oposta desde
o USR-449: um control store configurado mas inacessível responde `503`, nunca um `200`/`404`
que pareça "sem tickets" — ver `docs/manual/fila.md`.

## Prontidao de segredos do piloto (USR-279 T4)

O produto real que fecha a prova da V0 e o piloto da mansaodolago.com (seção acima do
`docs/FASE_ATUAL.md`). Antes de o grill de M1 abrir, `core.line.pilot_secrets` confere se as
credenciais externas do piloto já estão no cofre (`core.infra.secrets_vault`), sem nunca ler ou
imprimir o valor — só presente/ausente por chave (ver `docs/manual/segredos.md` para a lista de
chaves e o passo a passo de cadastro):

```powershell
python -m core.line.pilot_secrets
```

Um piloto sem credencial não assume valor nem segue com mock: `require_pilot_secret` é o único
jeito do código do piloto obter um segredo e levanta `PilotSecretMissingError` quando a chave
falta — falha fechada, igual ao resto da fábrica.

## Rodar localmente

```powershell
python -m pytest tests/test_line_v0_proof.py tests/test_line_acceptance.py tests/test_line_pilot_secrets.py -q
```
