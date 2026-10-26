<!-- covers: core/git/app_token.py, core/git/autonomy.py -->
# Merge autonomo com GitHub App (USR-343)

A fabrica faz push e merge com um token de curta duracao de uma GitHub App, sem a conta admin do owner.

## Variaveis (somente no processo executor da entrega, nunca no ambiente dos agentes de codigo)

| Variavel | Valor |
| --- | --- |
| `GH_APP_ID` | ID numerico da App (pagina da App, "App ID") |
| `GH_APP_INSTALLATION_ID` | numero final da URL de instalacao em Settings > Applications > Installed GitHub Apps |
| `GH_APP_PRIVATE_KEY` | conteudo do `.pem` gerado em "Generate a private key" (aceita `\n` literal) |

## Passo a passo (owner, uma vez)

1. GitHub > Settings > Developer settings > GitHub Apps > New GitHub App.
2. Nome `darkfac-factory`; Homepage qualquer URL; desmarque Webhook > Active.
3. Repository permissions: Contents = Read and write; Pull requests = Read and write; Metadata = Read-only.
4. "Where can this app be installed" = Only on this account. Create GitHub App.
5. Em "Private keys", clique Generate a private key e guarde o `.pem`.
6. Install App > escolha apenas o repositorio DarkFac.
7. Cadastre as tres variaveis no cofre de segredos da fabrica (processo executor).

## Comportamento

- `core/git/app_token.py` assina um JWT RS256 (9 min), troca por um token de instalacao restrito a
  `contents:write` e `pull_requests:write` e o guarda em cache ate 5 min antes de expirar.
- `core/git/autonomy.py` injeta o token como `GH_TOKEN` somente nos subprocessos `git push` e
  `gh pr create|merge`. Sem as tres variaveis (ou se a troca falhar) vale a autenticacao atual do `gh`.
- O padrao `^GH_APP_` em `.factory/config/agent_env_policy.json` mantem as variaveis fora dos agentes.
- Para o merge sem `--admin`, o ruleset do `main` deve listar a App como bypass actor (ou exigir apenas os checks).

## Entrega da branch (USR-385)

`GitAutonomyManager.deliver_branch` continua o contrato publico: commit, push, abertura de PR, merge squash e limpeza. `merge=False` abre o PR e retem merge e limpeza (aceite comercial). USR-385 partiu essa sequencia; o `DeliveryReport` (ok, action, pr_url, branch, message) permanece o mesmo.

1. `_prepare_delivery` valida o tipo e garante a branch `ticket/<id>` quando o trabalho esta em `main`, `HEAD` ou detached. `queue` so aceita arquivos de ledger e commita se a arvore esta suja. `implementation` recusa diff so de ledger e registra a conclusao. `registry` recusa arquivo fora da allowlist de modelos e commita sem mexer no ledger de demandas.
2. `_synchronize_delivery` faz fetch de `origin`, recusa colisao de ledger, alinha a base local quando ela ja esta contida na branch e sincroniza com `origin/<base>`. Branch sem commit alem da base devolve `no_changes` (e limpa se `merge` esta ligado). Depois do sync, o diff volta a ser conferido contra o contrato do tipo.
3. `_open_delivery_pr` faz push (`--force-with-lease`; `--force` so para atualizar a head de um PR de fila ja aberto com o mesmo titulo) e cria ou reaproveita o PR. Ajusta titulo e label (`queue`, `implementation` ou `registry`). Implementacao tambem registra o catalogo.
4. Com `merge` ligado, `_deliver` so chama `gh pr merge --squash --delete-branch` depois do portao de CI no head exato. Qualquer check em `fail` bloqueia o merge, incluindo `ledger-fast` e `pr-validation`; o caminho leve de ledger nao ignora os checks. Se nao houver workflow configurado, a ausencia de checks so e aceita depois da janela de tolerancia. Se o ruleset bloquear, o portao roda de novo e ha uma segunda tentativa de merge. Se o PR nao estiver mergeable, a branch e reaplicada em `origin/<base>` e o portao roda de novo, ate 3 vezes. O SHA do merge precisa aparecer em `origin/<base>` antes da limpeza da branch.

## Ledger de acoes do owner

`core/owner_actions/store.py` trata leituras de forma tolerante para manter a fila disponivel, mas toda mutacao valida o documento completo antes de gravar. Cada item precisa obedecer ao contrato de `OwnerAction` (incluindo `kind=action|decision`), e IDs duplicados, envelopes invalidos ou linhas malformadas fazem a escrita falhar sem substituir o arquivo. Assim, um item legado invalido como `OA-071` com `kind=approval` nao pode ser propagado por uma nova gravacao. As mesmas validacoes se aplicam ao ledger de controle e ao overlay de resolucoes.

`complete_ticket` grava o ticket como concluido no `DemandsStore` e, com `auto_push`, entrega a branch. A sequencia interna:

1. `_inspect_completion_changes` exige, contra `origin/main`, algum arquivo alem de `.factory/demands/demands.json`.
2. `_commit_completion` grava a conclusao e commita quando `auto_commit` esta ligado. Sem esse commit a conclusao e recusada.
3. `_deliver_completion` chama `deliver_branch` com `merge` desligado quando o projeto tem `requires_commercial_acceptance`. Sem `gh` autenticado, cai em push direto para a base.
4. `_verify_live_completion` roda so com `is_live_deploy`. Falha de convergencia reabre o ticket (`requeue_ticket`) e abre um defeito `planned` dependente dele.
