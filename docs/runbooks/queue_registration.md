# Registro de fila (`--queue-only`): PRs que nao mergeiam (USR-238)

Um registro de fila e um PR `chore(backlog): registrar USR-NNN - ...` (label `queue`) que so acrescenta uma linha a
`.factory/demands/demands.json`. Ele e aberto por `python C:\dev\DarkFac\run_ticket.py --create --queue-only ...`
(`run_ticket._queue_ticket_isolated` -> `GitAutonomyManager.deliver_branch(kind="queue")`).

## Diagnostico dos PRs #225, #245 e #276 (11/10/2026)

Leitura apenas (`gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup,commits`, `gh run list --branch ...`,
`git log origin/main`). Os tres PRs tem a mesma linha do tempo:

| PR | Ticket | Aberto (UTC) | Merge de `origin/main` na branch | CI do 2o push verde | Merge de outro PR que tocou `demands.json` na 2a janela de CI |
|----|--------|--------------|----------------------------------|---------------------|----------------------------------------------------------------|
| #225 | USR-179 | 08/10 22:35 | 22:54 (`1e28828`) | 23:09 | #215 (USR-134) as 23:04 |
| #245 | USR-192 | 09/10 20:05 | 20:21 (`67503a4`) | 20:36 | #242 (USR-190) as 20:27 |
| #276 | USR-222 | 10/10 18:34 | 18:49 (`b49a194`) | 19:04 | #278 (USR-223) as 18:59 |

O que aconteceu, em ordem:

1. A entrega abre o PR e espera os checks obrigatorios (ruleset `main-requer-ci-verde`: `pr-validation (ubuntu-latest)`,
   `pr-validation (windows-latest)`, `trusted-pr-policy`). O job do Windows leva **15 a 18 minutos**.
2. Durante essa janela outro PR que tambem altera `demands.json` entra na main (#224, #243 e #268 nos tres casos).
   Todo registro acrescenta uma linha no fim da mesma lista JSON, entao o git acusa conflito textual e o GitHub passa
   o PR a `CONFLICTING`/`DIRTY`.
3. Com o CI verde, `gh pr merge` falha com "not mergeable". A entrega faz **uma** nova sincronizacao: o merge semantico
   de `demands.json` (`core.demands.merge`) funciona, a branch recebe o commit "Merge remote-tracking branch
   'origin/main'" e e empurrada. Isso e um head novo, que exige **outra** rodada de CI de 15 minutos.
4. Nessa segunda janela entra de novo na main um PR que toca `demands.json` (#215, #242, #278). O segundo
   `gh pr merge` falha pelo mesmo motivo, e como o retry era unico a entrega devolvia erro. O PR ficava aberto, sem
   alerta, e dai em diante so acumulava conflitos (hoje `mergeable=CONFLICTING`, `mergeStateStatus=DIRTY`).
5. O status `UNKNOWN` visto antes no GitHub e so o calculo preguicoso de mergeabilidade; nao havia check vermelho nem
   revisao pendente: todos os checks dos tres PRs estao verdes.

Causas raiz: (a) conflito textual garantido entre registros concorrentes; (b) cada ressincronizacao custa uma rodada
inteira de CI, longa o bastante para a main andar de novo; (c) so um retry; (d) nenhum alerta quando a entrega desistia.

## O que mudou (USR-238)

* `core/git/autonomy.py`: ate `_MERGE_RESYNC_ATTEMPTS` (3) ressincronizacoes. Cada uma refaz o commit sobre a
  `origin/main` mais nova com o merge semantico de `demands.json`: os tickets que ja estao la sao preservados e o novo e
  acrescentado. Escolhida por ser a menor mudanca que fecha o buraco, sem mudar o formato do ledger nem os workflows.
  Ordenar a insercao nao resolve: qualquer posicao em que dois PRs insiram colide no diff textual.
* `core/git/queue_registration.py`: quando a entrega de um registro desiste com o PR aberto, abre o item
  `OA-QR-<numero do PR>` no backlog de acoes do owner (DarkHub), com o motivo. Alem disso, cada registro
  novo roda o vigia `check_stalled_queue_prs`: todo PR `queue` aberto ha mais de 30 min recebe o mesmo item (mesmo se o
  processo que o abriu morreu) e itens de PRs que ja nao estao abertos sao fechados sozinhos.
* `tests/test_queue_registration.py`: duas entregas concorrentes (a main muda duas vezes durante o CI) terminam com os
  dois tickets na main; entrega que nunca mergeia deixa o alerta; vigia (limiar, idempotencia, `gh` fora do ar).

## Checkout sujo pelo proprio alerta (USR-246)

Em 12/10/2026 o PR de registro #317 ficou preso e o mecanismo acima gravou `OA-QR-317` direto em
`.factory/owner_actions/owner_actions.json` **dentro do checkout compartilhado**, sem commit: o
`OwnerActionStore()` default resolve o caminho a partir de onde o modulo `core/owner_actions/store.py`
foi importado (`C:\dev\DarkFac`, ja que `run_ticket.py` e sempre invocado de la), nao do `cwd`/worktree
que a entrega estava usando. Ninguem commita esse arquivo, entao `run_ticket <ID>` seguinte recusava
com `SharedCheckoutDirty` -- o alerta que deveria ajudar virava a causa de um resgate manual, e o item
tambem nunca chegava a main (o Hub le o arquivo do commit, nao o working tree de ninguem).

Correcao: `.factory/owner_actions/` entrou em `LEDGER_PREFIXES` (`core/git/ci_checks.is_ledger_only`),
junto de `.factory/demands/` e `.factory/roadmap/`. `raise_alert`/`check_stalled_queue_prs` agora, sem
um `store` explicito (so os testes injetam um), abrem seu proprio worktree descartavel a partir de
`origin/main` (`core.git.queue_registration._commit_owner_action_changes`) e entregam a mutacao como
um PR `queue` autonomo igual ao de um registro de ticket -- nunca escrevendo no checkout de onde a
chamada partiu. `GitAutonomyManager.deliver_branch` ganhou `supervise_queue=False` para essa entrega
interna, senao ela re-executaria o proprio vigia sobre si mesma (recursao).

## Limites conhecidos

* Tres tentativas nao garantem o merge numa main muito movimentada: cada uma custa ~15 min de CI. O alerta cobre esse
  caso. A solucao definitiva e um caminho rapido de CI para PRs que mudam so o ledger (`.github/workflows/*` e arquivo
  protegido, entao depende do owner) ou o merge em fila do GitHub (merge queue).
* O vigia roda quando um novo registro e entregue; nao e um agendador. Se nenhum registro novo acontecer, rode-o a mao:
  `python -c "from pathlib import Path; from core.git.autonomy import _run_gh; from core.git.queue_registration import check_stalled_queue_prs; print(check_stalled_queue_prs(Path(r'C:\dev\DarkFac'), _run_gh))"`

## Resgate manual de um PR de registro (sem editar JSON a mao)

Usado quando existe o item `OA-QR-<n>` no DarkHub. O agente que for resgatar faz:

1. Ler o motivo: `gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup,commits`.
2. Numa worktree limpa a partir da main atual, aplicar a linha do ticket vinda da branch do PR com o merge semantico
   (`core.demands.merge.merge_demands_3way`, mesma funcao da entrega) e abrir um PR novo `chore(backlog)` com o label
   `queue`.
3. Esperar o CI, mergear (squash), fechar o PR antigo e marcar o `OA-QR-<n>` como `done`
   (`python C:\dev\DarkFac\scripts\owner_action.py done OA-QR-<n>`).
