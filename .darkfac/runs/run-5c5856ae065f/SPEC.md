# SPEC

## Objetivo
Provar a entrega autônoma do piloto greenfield stand-alone pilot-hello (página estática + /healthz, Python 3.12+, stdlib) em repositório privado GCamposGit/pilot-hello e ambiente Dokploy separado do darkfac-core, com evidências reais vinculadas ao SHA e independência total do runtime do core.

## Fora de escopo
- Contratar serviços novos ou criar infraestrutura adicional
- Inventar decisões de negócio do produto
- Alterar o core darkfac ou depender de seu runtime/serviços
- Reabrir as decisões D1–D5 já resolvidas pela OA-042
- Recriar recursos dos marcos M1 e M2 já evidenciados
- Implementar entrada real pelo Telegram (acompanhada pela OA-047)

## Desenho
O piloto é um app Python 3.12+ usando apenas biblioteca padrão no runtime, com uma página estática única e endpoint /healthz. O repositório privado GCamposGit/pilot-hello é independente do checkout DarkFac; o deploy ocorre em projeto Dokploy separado do darkfac-core, publicado pelo domínio da infraestrutura existente (opção A). O descritor do projeto registra setup/validate/build/smoke; o comando de testes é 'validate'. A independência é demonstrada por clone limpo fora do checkout DarkFac, com instalação, testes, wheel e execução sem imports ou serviços do core. M1 e M2 já possuem evidências registradas e devem ser verificados e reutilizados; os marcos M3–M7 são retomados sem recriar recursos. Cada marco tem até três critérios e conclusão exige evidências reais de revisão independente, PR, CI, merge, deploy e smoke vinculadas ao SHA.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- README.md
- pyproject.toml
- src/pilot_hello/__init__.py
- src/pilot_hello/app.py
- src/pilot_hello/static/index.html
- tests/test_app.py
- tests/test_healthz.py
- scripts/validate.py
- Dockerfile
- dokploy/docker-compose.yml
- .github/workflows/ci.yml

## Riscos
- Divergência entre o descritor do projeto e o comando real de validate/build
- Dependência acidental de imports ou serviços do darkfac-core no piloto
- Falha de configuração de webhook/deploy no Dokploy separado (acompanhada pela OA-043)
- Evidências de M1/M2 não reutilizáveis ou inconsistentes com o SHA atual
- Clone limpo fora do checkout DarkFac revelar dependência implícita de ambiente
- Smoke pós-deploy não responder 2xx por configuração de domínio/rota
