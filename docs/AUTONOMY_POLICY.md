# DarkFac delivery and autonomy policy (DF-20)

Esta política define quando uma tarefa pode entrar na fila de merge automatizada.
Ela é uma decisão local, determinística e baseada em evidência atual; o adaptador
GitHub apenas lê e normaliza dados da pull request.

## Classes de risco

| Classe | Regra de entrega |
| --- | --- |
| A | Baixo risco e reversível; pode entrar na fila quando os gates passam. |
| B | Código de baixo risco; pode entrar na fila quando os gates passam e a PR não é draft. |
| C | Autenticação, dados, dependências críticas ou governança; exige revisão manual. |
| D | Produção, credenciais amplas ou gasto fora do envelope; fora da automação. |

As classes A e B são elegíveis por padrão. Isso não é uma autorização de merge: a
fila só aceita uma decisão `eligible` com a PR aberta, não-draft, mergeabilidade
confirmada e todos os checks requeridos concluídos com `success`.

## Evidência vinculada ao SHA

Cada `DeliveryRequest` contém `base_sha`, `candidate_sha`, repositório, número da
PR, classe de risco, checks obrigatórios e uma chave de idempotência. A política
compara esses valores com um `PullRequestSnapshot` atual. Um check verde em SHA
antigo é classificado como `stale_checks` e nunca libera o SHA candidato. Também
são bloqueados base divergente, head divergente, PR fechada, draft, mergeabilidade
desconhecida e qualquer check ausente ou falho.

## Merge queue e idempotência

`MergeQueue` aceita somente decisões elegíveis. A chave de idempotência mapeia para
um fingerprint SHA-256 do pedido completo. Repetir exatamente o mesmo pedido
retorna o mesmo `queue_id` e incrementa `replay_count`; reutilizar a chave com
outro pedido falha com `IdempotencyConflictError`. O ledger JSON opcional é escrito
por substituição atômica e uma corrupção não é tratada como fila vazia.

O contrato não promete exactly-once no provedor externo. Ele impede duplicação
local e exige reconciliação posterior caso o processo caia depois de uma operação
remota.

## Limites

- O módulo não faz merge, push, criação de PR ou deploy.
- Tokens ficam somente no adaptador e nunca aparecem em mensagens de erro.
- Testes usam transporte GitHub injetado; nenhuma credencial ou rede é necessária.
- Projetos locais de demonstração continuam fora do escopo compartilhado.
