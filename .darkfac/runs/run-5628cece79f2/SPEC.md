# SPEC

## Objetivo
Reconciliar a baseline entregue e o registro canônico de USR-449 com evidência auditável do control store, documentando lacunas como tickets planned antes de qualquer componente novo.

## Fora de escopo
- Implementar QualityCycle, core/security ou qualquer componente novo de segurança.
- Criar agendador, fila, catálogo, roteador ou portal concorrente.
- Alterar retenção, limites de custo, alertas, perfis de acesso ou permissões por projeto.
- Usar GitHub, PR #846 ou HANDOFF para inferir o estado de USR-449 quando o control store estiver indisponível.

## Desenho
Fixar o SHA auditado, ler primeiro o registro USR-449 no control store e registrar uma projeção sanitizada da leitura. Inventariar código e histórico para separar capacidades entregues de referências apenas planejadas, comparando CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry e roteador com a ausência de QualityCycle e core/security. Corrigir USR-449 pelo mecanismo existente do store, preservando sua autoridade, e registrar como planned as lacunas não duplicadas. Se o store não puder ser lido, encerrar a reconciliação como bloqueada, com causa visível e sem inferência por fontes auxiliares.

## Arquivos a tocar
- .factory/demands/demands.json
- docs/reports/USR-449-baseline-reconciliation.md
- docs/FASE_ATUAL.md

## Riscos
- Uma atualização concorrente do control store pode tornar a leitura inicial obsoleta; é necessário reler e confirmar a gravação.
- PR #846, HANDOFF e referências históricas podem estar desatualizados e devem permanecer evidências auxiliares.
- core/security foi removido deliberadamente por USR-377; recriá-lo durante esta reconciliação violaria o escopo.
- Lacunas podem já possuir tickets equivalentes; criar novos registros sem busca prévia duplicaria o backlog.
- Mudança de origin/main entre inventário e entrega pode invalidar a matriz de baseline.
