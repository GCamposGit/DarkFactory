# GRILL

## Premissas
- Esta rodada só lê; a execução posterior corrige o registro de USR-449 e registra tickets, sem criar componentes novos de segurança.
- Reutilizar CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store; a elegibilidade por cadastro não habilita perfil nem acesso.
- Retenção permanente, limites de custo por projeto mais tetos globais e alertas no DarkHub e Telegram já foram decididos pelo owner e valem para entregas futuras; esta demanda não os altera.
- Nenhum segredo ou conta de terceiros é necessário: a reconciliação usa repositório, store de controle e GitHub já acessíveis.
- Mudança pontual de reconciliação e documentação, não produto novo.
- (technical) Qual é o formato e o destino da evidência de reconciliação? -> **Relatório versionado em docs/ (baseline entregue vs dependências citadas) mais atualização de docs/FASE_ATUAL.md, com os defeitos como tickets planned** (AGENTS.md §1.6 e §7 exigem documentação na mesma entrega e proíbem apenas mencionar defeitos. Um relatório auditável serve de base para a próxima rodada.)
- (technical) Se o store de controle estiver indisponível ou ilegível durante a reconciliação, como proceder? -> **Falhar fechado: marcar a reconciliação de USR-449 como bloqueada com motivo visível e não inferir o estado por outras fontes** (A demanda exige evidência de leitura do store. Inferir por outra fonte contradiz a premissa de não concluir que o store está disponível e esconderia a falha.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Qual é a fonte de verdade para reconciliar USR-449 quando o registro divergir entre o store de controle (.factory/demands/demands.json), o HANDOFF (PR #846 com CI vermelho, blocked deploy_evidence_missing) e o GitHub? (id=q1)
- [intent] Se QualityCycle e core/security realmente não existirem na base entregue, o que acontece com as dependências citadas em rodadas anteriores? (id=q2)
