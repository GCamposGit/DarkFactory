# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Hub sem queda: imagem GHCR e troca start-first (USR-207 parte 2)

## Problema
OA-044 cumprida pelo owner em 21/10 (autoDeploy do compose Darkhub desligado, registry ghcr.io criado no Dokploy, sonda de disponibilidade agendada no Notebook). Falta a parte de codigo de docs/proposals/USR-207-zero-downtime.md secoes 2.1-2.2: aplicar USR-207-ghcr-build.patch (workflow protegido hub-image.yml), trocar build por image ghcr.io/gcamposgit/darkhub:SHA no compose do Hub, adaptar core/orchestrator/compose_render.py e o teste test_darkhub_build_pinned_before_deploy_and_health_converges, redeploy esperar a tag do sha, remover container_name fixo e usar start-first gated por healthcheck. Corrigir tambem o comando schtasks da secao 1 da proposta (aspas quebram no PowerShell; usar Register-ScheduledTask com pythonw.exe em caminho absoluto).

## Jornada
OA-044 cumprida pelo owner em 21/10 (autoDeploy do compose Darkhub desligado, registry ghcr.io criado no Dokploy, sonda de disponibilidade agendada no Notebook). Falta a parte de codigo de docs/proposals/USR-207-zero-downtime.md secoes 2.1-2.2: aplicar USR-207-ghcr-build.patch (workflow protegido hub-image.yml), trocar build por image ghcr.io/gcamposgit/darkhub:SHA no compose do Hub, adaptar core/orchestrator/compose_render.py e o teste test_darkhub_build_pinned_before_deploy_and_health_converges, redeploy esperar a tag do sha, remover container_name fixo e usar start-first gated por healthcheck. Corrigir tambem o comando schtasks da secao 1 da proposta (aspas quebram no PowerShell; usar Register-ScheduledTask com pythonw.exe em caminho absoluto).

## Fora de escopo
- Nao aumentar a VPS sem medicao

## Criterios de aceite
- Um merge gera uma unica imagem GHCR do sha e um unico deploy do Hub
- Durante um deploy real a sonda de 1 s nao ve resposta diferente de 200 em /health
- Comando de agendamento da sonda na proposta funciona colado no PowerShell
- Portao oficial runner.py --quick passa
