# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Worker de teste se auto-recupera quando fica busy com CPU ociosa

## Problema
Em 21/10 o grokbox respondeu busy=True, active_runs=1, queue_length=1 por mais de 45 min com CPU 2,4%; o portao do owner falhou como 'unstable/unreachable' ate o owner chamar /system/update e /system/restart manualmente. Mesma familia do incidente de 15/10 (OA-023).

## Jornada
Em 21/10 o grokbox respondeu busy=True, active_runs=1, queue_length=1 por mais de 45 min com CPU 2,4%; o portao do owner falhou como 'unstable/unreachable' ate o owner chamar /system/update e /system/restart manualmente. Mesma familia do incidente de 15/10 (OA-023).

## Fora de escopo
- Nao matar job com progresso real

## Criterios de aceite
- Worker com job ativo sem progresso (CPU < 5% e sem saida de log) por 10 min marca o job como perdido e se reinicia sozinho
- O dispatcher reenfileira o job perdido no mesmo ou em outro worker com [REMOTE_RETRY]
- Evento registrado na telemetria e alerta no Telegram ops
- Portao oficial runner.py --quick passa
