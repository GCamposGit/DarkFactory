# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Supervisor do grokbox nao recarrega o worker ocioso quando o HEAD muda e nao esta versionado no repositorio

## Problema
Em 18/10/2026 ~17:20 BRT (OA-035), depois de 'git pull --ff-only' para be1bb6f no /workspace/darkfac/DarkFactory do grokbox, o script supervisor da VM (PID 198590, watchdog PID 8616) nao recarregou o worker ocioso em ~3 min, embora tivesse recarregado as 16:16. Foi preciso SIGTERM manual no worker (PID 417103); o supervisor o relancou em 1 s (instancia c57b77c5, /health git_sha be1bb6f). O supervisor vive so na VM, fora do repositorio, e o runbook docs/runbooks/grokbox_test_worker.md descrevia systemd, que a VM nao tem. Sem recarga confiavel, o grokbox fica em codigo antigo e correcoes como USR-298 nao chegam ao portao.

## Jornada
Supervisor do grokbox nao recarrega o worker ocioso quando o HEAD muda e nao esta versionado no repositorio

## Fora de escopo
- Nao instalar systemd na VM.
- Nao reiniciar worker com job em andamento.

## Criterios de aceite
- O script supervisor do grokbox passa a ser versionado em scripts/ e o runbook aponta para ele.
- Com o worker ocioso (busy=false), uma mudanca de HEAD em project_root faz o worker ser relancado em ate 60 s e o /health passar a mostrar o git_sha novo; com busy=true, a recarga espera o job terminar.
- Teste automatizado da logica de decisao (HEAD mudou x ocioso/ocupado) sem depender da VM.
