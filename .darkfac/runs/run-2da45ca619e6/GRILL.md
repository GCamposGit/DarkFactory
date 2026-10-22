# GRILL

## Premissas
- O repositorio atual e o darkfac (ou equivalente) com run_ticket.py, deliver_parallel.py, runner_grok, runner_local e scheduler.
- A investigacao sera conduzida com relogio monotonic e instrumentacao sem alterar comportamento de producao.
- Erros estruturados de sondagem externa serao definidos como JSON com campos de provedor, duracao e causa.
- A fila ativa e myqueue; queue.txt e considerada aposentada conforme inspecao de deliver_parallel.py.
- O owner ja autorizou o envio de diff privado USR-321 ao Grok (OA-048), portanto nao ha pergunta de secret/account bloqueante sobre isso.
- Nao ha premissas anteriores nem licoes recentes registradas para esta fabrica.
- A rodada e de investigacao pontual, nao um produto novo (is_product_scale=false).
- (technical) O piso de cota local (5.5GB no runner_local vs 7.5GB no scheduler) deve ser unificado nesta rodada ou apenas reportado como divergencia? -> **A** (O criterio 'Limitar despacho local a capacidade verificada' e o scheduler 7.5GB e a referencia mais conservadora; unificar em 7.5GB preserva workers ocupados e evita overcommit, alinhado ao fora-de-escopo de nao relaxar piso.)

## Decisoes do owner
- [intent] O escopo desta rodada inclui corrigir os problemas de fila/ledger/gate local (rc7, reenqueue sem mutex, kind ledger-only, piso 5.5GB vs 7.5GB) ou apenas investigar e documentar a causa da demora do preflight de cota/wake? -> **C: Investigar preflight agora e abrir demandas separadas para os bugs de fila/ledger/gate** (respondida pelo owner)
- [business] Qual e o prazo (timeout) aceitavel para sondagens externas de cota/wake falharem com erro estruturado? -> **D** (default_after_timeout)
- [intent] A revisao independente USR-321 pelo Grok (opcao A autorizada pelo owner) faz parte desta rodada de grill/planejamento ou e um fluxo paralelo ja em andamento? -> **A** (default_after_timeout)

## Aguardando decisao do owner
- [account] Os scripts operacionais em C:/Users/guigc/darkfac-ops (fora do repo) podem ser lidos/inspecionados nesta investigacao, ou o escopo se limita ao repositorio atual? (id=q4)
