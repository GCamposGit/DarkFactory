# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Atrito zero: fila no control store, guard enxuto com aprovacao de um toque, GitHub App e cofre de segredos

## Problema
Cerca de 80% das 70 acoes do owner desde 09/10 foram criadas pela propria fabrica: 23 rituais de publicar arquivo protegido com merge --admin ou push direto (core/orchestrator/guard.py protege core/harness/*, que e codigo operacional), PRs de registro de fila e de OA que nao mergeiam (39 PRs ticket/queue-* e 22 chore/oa-* entre os ultimos 100), chaves recadastradas e segredos espalhados entre .env, registro do Windows e Dokploy. O merge autonomo depende do gh do owner com permissao de admin. Este bloco absorve USR-370 (isolamento de credenciais do agente) e USR-374 (owner_actions.json local bloqueia o node_sync). Plano: docs/plans/PLANO_V0_2026-10-24.md.

## Jornada
Fila de tickets e acoes do owner passam a ter como fonte da verdade o control store Postgres que ja existe (core/workflow/control_store.py e adapter Postgres), com API unica de leitura e escrita usada por run_ticket --create, Hub, Telegram e chat
Um job diario exporta snapshot de .factory/demands/demands.json e .factory/owner_actions/owner_actions.json para o git em um unico commit; nenhum registro de ticket ou OA abre PR
guard.py passa a proteger apenas MISSION.md, FACTORY_RULES.md e AGENTS.md; mudanca nesses arquivos gera pedido de aprovacao no Telegram (bot do owner) com o diff resumido; o toque do owner grava autorizacao assinada que o CI verifica; core/governance/authorization.py deixa de aceitar a string 'owner' sem assinatura
Push e merge autonomos usam uma GitHub App com token de curta duracao e escopo contents/pull_requests apenas neste repositorio, fora da lista de bypass
Segredos ficam num cofre unico (SOPS+age no repositorio ou equivalente gratuito escolhido por ADR) lido por todas as maquinas; script de bootstrap faz o owner cadastrar cada segredo uma unica vez
Agentes de codigo rodam sem acesso ao cofre (filtro de ambiente por allowlist em core/line/agent_env.py); push, merge e deploy sao feitos por um processo separado (broker) com credencial de curta duracao
Acao do owner da mesma categoria repetida duas vezes gera automaticamente um ticket de automacao

## Fora de escopo
- Nao trocar Postgres por outro banco
- Nao criar interface grafica nova para o cofre
- Nao mudar o fluxo de aceite comercial (requires_commercial_acceptance)

## Criterios de aceite
- Registrar um ticket pelo Hub, pelo Telegram e por run_ticket --create nao abre PR; o ticket aparece na fila do Hub em segundos e no snapshot diario do git
- Ultimos 7 dias apos o deploy: zero PRs ticket/queue-* e chore/oa-*
- PR que altera core/harness/* mergeia sem --admin e sem acao do owner
- PR que altera MISSION.md, FACTORY_RULES.md ou AGENTS.md so mergeia apos aprovacao do owner pelo Telegram; autorizacao sem assinatura valida e rejeitada pelo CI (teste)
- Merge autonomo funciona com a GitHub App; a conta admin do owner nao e usada por nenhum processo da fabrica
- Agente de desenvolvimento nao consegue ler GITHUB_TOKEN, DOKPLOY_API_KEY, tokens do Telegram nem o arquivo .env (teste que tenta ler e falha)
- Script de bootstrap configura uma maquina nova a partir do cofre sem pedir segredo ja cadastrado
- Hub mostra contagem semanal de acoes do owner por categoria; OA repetida gera ticket de automacao (teste)
- Manual do usuario e documentacao tecnica (docs/manual/) atualizados na mesma entrega, conferidos contra o codigo
- Saldo de testes da suite nao aumenta: teste novo de comportamento substitui ou consolida testes existentes; nenhum teste de forma entra no portao
- Learning pack da entrega (leigo + dev) publicado e enviado ao owner
- Portao oficial verde, revisao por outra familia, merge e deploy com smoke verde
