# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Hub cai a cada merge: redeploy sem downtime, deploy deduplicado e frontend resiliente

## Problema
O owner viu o Hub sair do ar durante testes em 10/10. O compose Darkhub (Dokploy wuH-sjZBig74xFGdk4IsL) teve 12 deploys em 10/10 (08:43 a 14:45), cada um reconstruindo a imagem na VPS (2 vCPU, 3,7 GB, RAM em 79%) e recriando o container darkhub-cloud-247 (stop-then-start, ~1 min fora). Ha deploy duplo: autoDeploy 'NEW CHANGES' em todo push no main mais 'Manual deployment' via scripts/dokploy_redeploy.py, mesmo quando o merge so mexeu no ledger. Durante a janela o frontend mostra erros crus como 'Falha na telemetria de infraestrutura: Failed to fetch' (hub/frontend/infra.js) sem nova tentativa. Limite de memoria do container: 512 MB. Diagnostico de 10/10/2026 (sessao interativa Claude Code, pedido do owner sobre o DarkHub).

CONGELADO em 11/10/2026 (plano V0, docs/plans/V0_OPERACIONAL_2026-10-11.md): fora do caminho critico da linha.

## Jornada
O owner viu o Hub sair do ar durante testes em 10/10. O compose Darkhub (Dokploy wuH-sjZBig74xFGdk4IsL) teve 12 deploys em 10/10 (08:43 a 14:45), cada um reconstruindo a imagem na VPS (2 vCPU, 3,7 GB, RAM em 79%) e recriando o container darkhub-cloud-247 (stop-then-start, ~1 min fora). Ha deploy duplo: autoDeploy 'NEW CHANGES' em todo push no main mais 'Manual deployment' via scripts/dokploy_redeploy.py, mesmo quando o merge so mexeu no ledger. Durante a janela o frontend mostra erros crus como 'Falha na telemetria de infraestrutura: Failed to fetch' (hub/frontend/infra.js) sem nova tentativa. Limite de memoria do container: 512 MB. Diagnostico de 10/10/2026 (sessao interativa Claude Code, pedido do owner sobre o DarkHub).

## Fora de escopo
- Nao trocar o Dokploy por outra plataforma de deploy.
- Nao aumentar a VPS sem medicao de memoria.

## Criterios de aceite
- Redeploy do Hub sem indisponibilidade: imagem construida fora da VPS (CI publica no GHCR) e troca gated por healthcheck (start-first ou blue/green via Traefik); medir com sonda de 1 s durante um deploy real: zero respostas diferentes de 200 em /health.
- Um merge gera no maximo um deploy do Hub, e merges que nao tocam no que a imagem embute nao reconstroem nada (ou atualizam so os dados sem recriar o container).
- Frontend: toda chamada fetch do Hub passa por um helper unico com retry/backoff; durante indisponibilidade aparece um aviso 'Hub atualizando, reconectando...' e os paineis se recuperam sozinhos, sem mensagem 'Failed to fetch'.
- Sonda externa de disponibilidade do Hub alerta no Telegram ops quando /health falha por mais de 2 min.
- Uso de memoria do container avaliado sob carga (todas as abas abertas + refresh) e limite ajustado com evidencia; OOMKilled monitorado.
