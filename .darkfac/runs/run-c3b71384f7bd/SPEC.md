# SPEC

## Objetivo
Ligar o deploy pós-merge do pilot-hello no Dokploy existente em projeto separado do darkfac-core, preencher o descritor de deploy e os smokes HTTP do projeto a partir da aplicação criada pelo owner (OA-043) e comprovar que GET /healthz responde 200 com identidade correspondente ao SHA mergeado, registrando evidência vinculada ao ticket do piloto.

## Fora de escopo
- Criação de repositório, infraestrutura ou projeto Dokploy novo
- Alteração do darkfac-core, seu allowlist ou seu script de redeploy
- Entrada real via Telegram (OA-047)
- Solicitação de novos acessos ou segredos
- Implementação de novas telas ou fluxos de produto
- Alteração de registros de decisão (OA-042/OA-043)

## Desenho
Reutilizar o descritor de deploy existente do projeto e o adaptador Dokploy já presente, apontando para a aplicação pilot-hello criada pelo owner em OA-043, em projeto Dokploy separado do darkfac-core. O deploy pós-merge deve ser disparado a partir do SHA completo resultante do merge de M4. O endpoint /healthz deve expor explicitamente a identidade do build (SHA mergeado) além de status ok e versão semver, permitindo verificação de correspondência. O smoke pós-deploy consulta GET /healthz, valida HTTP 200, status ok e identidade igual ao SHA esperado; divergência ou ausência de identidade impede declarar M5 concluído. A evidência final registra URL consultada, SHA esperado e observado, saída do smoke e horário UTC, sem credenciais, vinculada ao ticket do piloto.

## Arquivos a tocar
- deploy/dokploy/pilot-hello.yaml (ou descritor equivalente do projeto)
- scripts/line_validate.py
- scripts/smoke_pilot_hello.py
- app/healthz.py (ou rota /healthz do pilot-hello)
- tests/test_healthz_identity.py
- tests/test_smoke_pilot_hello.py
- docs/evidence/M5-pilot-hello.md

## Riscos
- Identificadores da aplicação, domínio e webhook de OA-043 podem não estar disponíveis ou divergir do esperado, bloqueando o deploy
- /healthz pode não expor o SHA mergeado, exigindo ajuste no app do piloto antes do smoke
- Ambiente Dokploy separado pode compartilhar rede/segredos com darkfac-core, quebrando isolamento
- Smoke pode passar com HTTP 200 e semver sem comprovar correspondência ao SHA, gerando falso positivo
- Evidência pode vazar credenciais se o script de smoke não filtrar headers/tokens
