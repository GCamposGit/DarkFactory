# DF-11 — Runtime e store de recuperação

## Decisão

O DF-11 escolhe um runtime Python pequeno sobre `sqlite3` da biblioteca padrão.
O banco padrão é `.factory/orchestrator.sqlite3`, separado do ledger JSON de
estado do DF-01 e do ledger de telemetria do DF-10.

Essa escolha atende o cenário atual: operação local, offline, Windows/Linux,
clone limpo e necessidade de testar explicitamente recuperação e fencing. Não
foi adicionada uma dependência de runtime.

## Spike executado

O spike exercita uma sequência curta e interrompível:

1. `worker-a` faz claim de `DF-11`.
2. A primeira etapa retorna um resultado e o runtime grava um checkpoint com
   `step_index=1`.
3. A segunda etapa falha simulando a queda do processo; o run permanece
   `RUNNING` com o último checkpoint durável.
4. A lease expira sem uma rotina de cleanup.
5. `worker-b` faz claim atômico do mesmo run, recebe novo `token` e
   `fencing_token`, e continua em `step_index=1`.
6. Uma tentativa posterior de conclusão usando a lease de `worker-a` é
   rejeitada; `worker-b` conclui o run.

Evidência automatizada: `tests/test_runtime_recovery.py` cobre exclusão mútua
de claims concorrentes, expiração/fencing, renovação e recuperação após crash.

## Comparação de alternativas

| Opção | Resultado do spike | Decisão |
| --- | --- | --- |
| Supervisor Python + SQLite | Zero dependências novas; `BEGIN IMMEDIATE` serializa claims entre processos; checkpoints e leases são inspecionáveis; teste determinístico com relógio injetável. | **Escolhida para DF-11.** |
| JSON atômico | Compatível com os ledgers existentes, mas não oferece transação de leitura-verificação-escrita entre workers sem inventar um lock adicional; o fencing ficaria mais frágil. | Não escolhida para o runtime. Mantida a separação com DF-01/DF-10. |
| LangGraph atrás da mesma interface | Poderia fornecer checkpointing e composição, mas adicionaria dependência e não substitui a política de lease/fencing do domínio. A implementação de dois runtimes completos aumentaria o escopo do spike. | Adaptador futuro, não requisito do clone limpo. |
| Serviço gerenciado | Tem operação externa, credenciais, custo e menor portabilidade; não é necessário para a execução local prevista nesta fase. | Fora do escopo. |

O critério de desempate foi a menor superfície operacional que ainda permite
provar a propriedade central do ticket: para uma tarefa há no máximo uma lease
válida, e somente o owner com token e fencing atuais pode renovar, checkpointar
ou concluir.

## Contrato de lease

Cada claim persiste:

- `owner`: identidade lógica do worker;
- `token`: nonce aleatório exclusivo daquela posse;
- `fencing_token`: contador monotônico por tarefa;
- `expires_at`: limite de validade, comparado em UTC;
- `run_id`: vínculo com o estado recuperável.

O claim usa `BEGIN IMMEDIATE`. Se existir uma lease não expirada, o segundo
worker recebe `LeaseConflictError`. Se a lease expirou ou foi liberada, a
substituição ocorre na mesma transação, incrementa o fencing e troca o token.
Toda mutação verifica os quatro vínculos (`task_id`, `owner`, `token` e
`fencing_token`) e a expiração; caso contrário, recebe `StaleLeaseError`.

O runtime renova antes e depois de cada etapa. O checkpoint só avança depois
que o callback retorna e o store aceita a escrita. Portanto, a queda entre
checkpoints retoma do último passo confirmado, sem declarar sucesso falso.

## Limites conhecidos

O contrato é de recuperação com semântica *at-least-once* para efeitos externos
dentro de uma etapa: um processo pode cair depois de um efeito externo e antes
do checkpoint local. Etapas que chamam serviços externos devem usar uma chave de
idempotência derivada de `run_id` e `step_index`; exatamente-once universal não
é prometido. Orçamento, deadlines e adaptadores de execução permanecem nos
próximos tickets DF-12/DF-13.

O SQLite escolhido é adequado para o supervisor local e para concorrência
moderada. Alta disponibilidade, replicação e múltiplos hosts exigiriam um
store transacional externo e uma decisão de arquitetura posterior.

## Compatibilidade e rollback

`core/orchestrator/state.py` continua dono do contrato de estados do DF-01 e
`core/usage/store.py` continua dono da telemetria/idempotência do DF-10. O
DF-11 não migra nem altera esses arquivos. Para desabilitar o runtime novo,
basta não despachar novas execuções para `.factory/orchestrator.sqlite3`; os
ledgers anteriores permanecem utilizáveis.

