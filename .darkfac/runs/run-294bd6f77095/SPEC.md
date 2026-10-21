# SPEC

## Objetivo
Provar a entrega autônoma do piloto greenfield stand-alone pilot-hello (página estática única + /healthz, Python 3.12+, stdlib no runtime) em repositório privado GCamposGit/pilot-hello e ambiente Dokploy separado do darkfac-core, retomando os marcos M3–M7 do plano docs/plans/USR-279-greenfield-pilot.md com evidências reais vinculadas ao SHA e sem dependência de runtime do core.

## Fora de escopo
- Contratar serviços novos ou criar infraestrutura adicional além do Dokploy existente (opção A).
- Inventar decisões de negócio do produto ou reabrir D1–D5 já resolvidas em OA-042.
- Recriar recursos/evidências de M1 e M2 já registrados.
- Repetir perguntas de acesso/deploy (OA-043) ou de entrada real pelo Telegram (OA-047).
- Alterar arquivos nesta etapa de planejamento (somente leitura).

## Desenho
O piloto é um projeto Python 3.12+ mínimo, sem banco, autenticação ou dependências de runtime além da biblioteca padrão, expondo uma página estática única e um endpoint /healthz. O repositório privado GCamposGit/pilot-hello é publicado em um projeto Dokploy separado de darkfac-core, usando o domínio da infraestrutura existente. A independência é demonstrada por clone limpo fora do checkout DarkFac, com instalação, testes, build de wheel e execução sem imports ou serviços do core; o comando de testes registrado no descritor é `validate` (python scripts/line_validate.py). O trabalho retoma M3–M7 do plano USR-279, mantendo até três critérios por marco e exigindo evidências reais de revisão independente, PR, CI, merge, deploy e smoke vinculadas ao SHA.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- pilot-hello/pyproject.toml
- pilot-hello/src/pilot_hello/__init__.py
- pilot-hello/src/pilot_hello/app.py
- pilot-hello/static/index.html
- pilot-hello/tests/test_app.py
- pilot-hello/tests/test_independence.py
- pilot-hello/scripts/line_validate.py
- pilot-hello/README.md
- pilot-hello/Dockerfile

## Riscos
- Divergência entre o plano USR-279 e o estado real de M1/M2, exigindo verificação antes de retomar M3.
- Dependência implícita do runtime do core (imports, serviços ou variáveis) que quebre a prova de independência no clone limpo.
- Configuração de deploy/webhook (OA-043) ou entrada Telegram (OA-047) indisponível no momento da execução, bloqueando smoke real.
- Evidências de CI/PR/merge não vinculadas ao SHA correto, invalidando o critério de aceite.
- Ambiente Dokploy compartilhado com darkfac-core causar acoplamento acidental se o projeto não estiver isolado.
