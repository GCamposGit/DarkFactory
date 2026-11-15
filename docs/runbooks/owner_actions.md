# Runbook - Backlog de acoes humanas do owner no DarkHub (USR-190)

Mandato do owner: tudo que **so o owner pode fazer ou decidir** fica registrado, com o passo a passo
completo, no DarkHub. Voce nunca precisa perguntar no chat "o que eu tenho de fazer?".

- Fonte unica: PostgreSQL (`DARKFAC_HF02_DATABASE_URL`, mesma base da fabrica).
- Tela: `https://darkhub.ggcampos.com` > menu lateral **Ações do Owner** (ou direto em
  `https://darkhub.ggcampos.com/#owner-actions`)
- API: `GET /api/interventions/priority` (junto com Grills, G8 e WAITING_HUMAN) e `GET /api/owner-actions`
- CLI para agentes: `python C:\dev\DarkFac\scripts\owner_action.py` (`add`, `list`, `done`, `answer`)

## 1. Como voce usa o DarkHub (tela a tela)

1. Abra `https://darkhub.ggcampos.com`. No topo da pagina, a faixa **Intervenções Pendentes** mostra o
   total e um botao **N Ação(ões) do Owner**; no menu lateral, o item **Ações do Owner** mostra um
   contador (vermelho quando existe item critico).
2. Clique no botao ou no item do menu. A secao **Ações do Owner** lista um cartao por item, os mais
   criticos primeiro. Use os botoes **Todas / Crítica / Alta / Média / Baixa** para filtrar.
3. Cada cartao mostra: prioridade, tipo (Ação ou Decisão), codigo `OA-NNN`, os tickets que ele
   **bloqueia**, o selo **Aguarda OA-xxx** (quando depende de outro item ainda aberto) e o selo
   **Libera OA-xxx** (quando outro item espera por ele). Clique no titulo para abrir/fechar os detalhes.
4. Dentro do cartao: **Por que**, os **passos numerados** (siga na ordem), os comandos em caixa escura com
   o botao **Copiar**, e **Como verificar**.
5. Quando terminar uma **Ação**, clique em **Marcar como feito** e confirme. O item some da fila; os
   itens que dependiam dele deixam de aparecer como bloqueados.
6. Para uma **Decisão**, marque uma opcao, escreva uma nota se quiser e clique em **Responder decisão**.
   A resposta e anexada aos tickets bloqueados (veja a secao 4) e a linha de producao retoma sozinha.

Se o backlog estiver ilegivel o painel mostra um aviso amarelo e o resto da fila continua funcionando.

## 2. Schema do arquivo

```json
{
  "schema_version": 1,
  "description": "...",
  "actions": [
    {
      "id": "OA-001",
      "kind": "action",
      "title": "Titulo curto e objetivo",
      "priority": "critical",
      "status": "open",
      "why": "Por que so o owner pode fazer e o que trava sem isso.",
      "blocks": ["USR-162"],
      "depends_on": [],
      "steps": [
        {"text": "Passo tela a tela.", "command": "comando absoluto opcional"}
      ],
      "verify": "Como saber que deu certo.",
      "created_at": "2026-10-09T22:00:00Z",
      "created_by": "claude-code",
      "resolved_at": null
    }
  ]
}
```

| Campo | Regra |
| --- | --- |
| `id` | `OA-NNN` sequencial (gerado pelo CLI). Itens espelhados da linha usam `OA-HR-<run>-<estagio>`. |
| `kind` | `action` (o owner executa passos, exige ao menos um) ou `decision` (exige 2 ou mais `options`). |
| `priority` | `critical`, `high`, `medium` ou `low`. A fila ordena nessa ordem; dentro da mesma prioridade, itens sem dependencia pendente vem antes. |
| `status` | `open`, `waiting` ou `done`. `resolved_at` so existe em `done`. |
| `blocks` | Tickets (`USR-NN`) que esperam por este item. Uma decisao respondida e anexada a eles. |
| `depends_on` | IDs `OA-...` que precisam estar `done` antes. Dependencia pendente vira `blocked_by` na API. |
| `options` | So em `decision`: `[{"id": "A", "label": "...", "detail": "..."}]`; ids unicos. |
| `answer` | Preenchido quando a decisao e respondida: `option_id`, `note`, `answered_at`, `answered_by`. |

Segredos nunca entram no arquivo: o schema **rejeita** tokens de bot, tokens do GitHub, chaves `sk-`,
chaves privadas e URLs com senha literal. Use placeholders como `<TOKEN_NOVO>` ou `USUARIO:SENHA`. O CLI
tambem remove qualquer coisa com formato de credencial do que imprime.

## 3. CLI para agentes (obrigatorio registrar no mesmo turno)

```powershell
python C:\dev\DarkFac\scripts\owner_action.py add --title "Colocar o token novo no Dokploy" --priority high --why "Token antigo revogado" --blocks USR-100 --step "Abra https://dokploy.ggcampos.com e entre no projeto darkfac-core" --step "Salve o token novo" --cmd "[Environment]::SetEnvironmentVariable('TELEGRAM_BOT_TOKEN', '<TOKEN_NOVO>', 'User')" --verify "O bot responde /status"
python C:\dev\DarkFac\scripts\owner_action.py list
python C:\dev\DarkFac\scripts\owner_action.py done OA-001
python C:\dev\DarkFac\scripts\owner_action.py answer OA-005 --option A --note "pode seguir"
```

- `--step` abre um passo; o `--cmd` seguinte pertence a esse passo (repetivel). Decisao: `--kind decision`
  e `--option "A=Rotulo|detalhe"` (repetivel, minimo duas).
- Rascunhos longos: `--from-json arquivo.json` (mesmo schema, sem `id` e timestamps).
- Criar item `critical` ou `high` avisa o owner no Telegram (mesmo mecanismo do aviso de Grill, bots
  owner e ops) com link para `#owner-actions`. Falha de envio nunca falha o comando; `--no-notify` desliga.
- O CLI grava o control store em producao. Nao ha commit de ledger nem redeploy para
  publicar uma OA. No modo local `file`, a escrita do arquivo e atomica; JSON corrompido
  aparece como aviso na leitura e impede gravacoes.
- `add` escolhe o proximo `OA-NNN` olhando o arquivo local **e** `origin/main` (`git show`, melhor esforco).
  Mesmo assim, duas branches podem gerar o mesmo id antes de sincronizar uma com a outra; se isso
  acontecer, `run_ticket`/`deliver_branch` renumeram sozinhos a OA da branch logo apos o rebase/sync,
  antes do portao (USR-255; secao 7).
- Quem resolve o bloqueio marca `done` (ou `answer`) e cita os `OA-xxx` no relatorio do turno.

## 4. Decisoes: como a linha retoma

`answer` (CLI) e **Responder decisão** (DarkHub) fazem tres coisas: gravam `answer` no item, marcam-no
`done` com `resolved_at` e, para cada ticket nao concluido em `blocks`, adicionam a tag
`owner-decision:OA-NNN=<opcao>` e um paragrafo `[Decisao do owner OA-NNN] ...` ao fim do
`problem_statement` (com a nota). Responder de novo substitui a marca em vez de empilhar. O ticket e
lido pelo planejamento da linha com essa decisao ja no texto.

## 5. Caminho do dado em producao

PostgreSQL e a fonte unica de OAs. CLI, Hub e HumanRequest gravam no mesmo
control store; um item aparece na proxima leitura do painel, sem redeploy.
O arquivo JSON e exportacao; o modo local exige configuracao file.

Configuracao, migracao unica dos overlays, HTTP 503 e verificacao apos deploy:
veja docs/manual/fila.md, secao Fonte unica de OAs no DarkHub.

## 6. HumanRequest da linha vira item do backlog

`core.line.human.request_human_help` (a linha parou e precisa do owner) agora tambem grava o pedido como
`owner_action` com os mesmos passos tela a tela do `guide_md` (listas numeradas viram passos; blocos de
codigo viram o comando do passo). O id e derivado do run e do estagio (`OA-HR-<run>-<estagio>`), entao
repetir o pedido nao cria outro cartao e um item ja fechado nao reabre. Quando a linha retoma o job
(`resume_blocked_job`), o item e marcado `done`. Tudo e best effort: uma falha aqui nunca derruba a linha,
e o aviso Telegram original da `HumanRequest` continua sendo o unico (o espelho nao envia outro).

## 7. Solucao de problemas

| Sintoma | Causa provavel | O que fazer |
| --- | --- | --- |
| Item novo nao aparece no DarkHub | Conexao ou modo do control store incorreto | `gh pr view <n> --json state`; depois `python C:\dev\DarkFac\scripts\dokploy_redeploy.py` |
| Aviso amarelo "JSON corrompido" | `owner_actions.json` com erro de sintaxe | Abra o arquivo, corrija o JSON (o CLI `list` indica a linha); a fila de outras intervencoes segue normal |
| `[ERRO] ... credential-shaped` no `add` | Passo com token/senha literal | Troque por placeholder (`<TOKEN_NOVO>`) |
| Cliquei em Marcar como feito e voltou 401 | Sessao do Hub expirada | Recarregue a pagina e tente de novo |
| Portao reprova com "item OA-NNN duplicado ignorado" | Duas branches escolheram o mesmo `OA-NNN` sem ver a commit uma da outra; o rebase/merge juntou as duas sem conflito textual | Normalmente ja autocorrigido: `run_ticket`/`deliver_branch` renumeram a OA da branch logo apos o sync com `origin/main`, antes do portao (USR-255). Se persistir, edite o arquivo a mao, renumere a entrada duplicada da sua branch e rode `python C:\dev\DarkFac\scripts\owner_action.py list` para conferir |

## 8. Publicacao de arquivo protegido

Substituida pela aprovacao por toque no Telegram (USR-343 S3): veja `docs/manual/governanca.md`, secao
"Aprovacao por toque". Nao ha mais push direto nem OA de publicacao protegida.
