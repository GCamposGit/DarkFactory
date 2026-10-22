# GRILL

## Premissas
- A mudança é pontual no núcleo DarkFac e no DarkHub, preservando Python, Pydantic v2, FastAPI na camada HTTP e frontend existente.
- O botão ficará em Ações do Owner, com diff, ticket, caminhos protegidos exatos e SHA do commit candidato visíveis antes do clique.
- A aprovação usará a autenticação de owner existente; credenciais de agentes e sincronização de máquinas não poderão emitir autorizações.
- O registro seguirá o contrato do USR-228 e será publicado na base confiável origin/main por um serviço confiável após o clique; um registro presente apenas na branch candidata não autoriza a publicação.
- A identidade aprovada será o SHA do commit, sem alternativa por tree hash; qualquer novo commit, inclusive rebase, exigirá nova aprovação.
- A esteira executará runner.py --quick com --remote-required em candidato limpo e isolado; indisponibilidade do worker manterá a publicação pendente, sem fallback local.
- Merge e deploy seguirão o ciclo autônomo existente, preservando revisão independente e checks obrigatórios, sem push direto ou merge --admin.
- Falha do portão bloqueará o merge; alterações para corrigir o candidato invalidarão a aprovação anterior.
- A implementação incluirá testes contra autoaprovação, registro no candidato e autorização baseada somente em variável de ambiente.
- Esta etapa é somente de leitura e esclarecimento; nenhum arquivo, ticket ou autorização será alterado.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Por quanto tempo a aprovação do owner deve valer para o SHA aprovado? (id=q1)
- [intent] O novo fluxo deve substituir também as OAs de publicação protegida que ainda estiverem abertas? (id=q2)
