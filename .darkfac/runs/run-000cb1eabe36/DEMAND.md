# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Ligar despacho local ao fleet_policy com capacidade verificada e reabertura de ticket concluido via requeue

## Problema
Residuo do USR-321 (21/10/2026): select_host_for_ticket/evaluate_admission (core/line/fleet_policy.py) aceitam verified_capacity mas nao tem chamador de producao, entao o despacho local nao respeita core.line.gate_ram.measure_workers(); e core/git/autonomy.py (~linha 1558, falha de convergencia ao vivo) reescreve status=planned a mao porque DemandsStore.requeue_ticket recusa tickets completed.

## Jornada
Residuo do USR-321 (21/10/2026): select_host_for_ticket/evaluate_admission (core/line/fleet_policy.py) aceitam verified_capacity mas nao tem chamador de producao, entao o despacho local nao respeita core.line.gate_ram.measure_workers(); e core/git/autonomy.py (~linha 1558, falha de convergencia ao vivo) reescreve status=planned a mao porque DemandsStore.requeue_ticket recusa tickets completed.

## Fora de escopo
- Nao criar scheduler paralelo nem relaxar piso de cota, G1 ou isolamento.

## Criterios de aceite
- Despacho local real consulta fleet_policy com verified_capacity de measure_workers e nunca excede a capacidade nem interrompe worker ocupado.
- requeue_ticket ganha modo explicito de reabertura (ex.: reopen=True com motivo) usado pela convergencia ao vivo, preservando historico; reescritas manuais de status saem do codigo.
- Testes focados cobrem os dois caminhos.
