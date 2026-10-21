# SPEC

## Objetivo
Retomar M3–M7 do USR-279 e provar entrega autônoma do pilot-hello, reutilizando M1/M2 e vinculando intake, revisão, PR, CI, merge, deploy e independência a evidências reais.

## Fora de escopo
- Recriar o repositório privado GCamposGit/pilot-hello ou repetir o bootstrap e o ambiente já comprovados.
- Novas contratações, domínio pago, banco, autenticação ou novas regras de negócio.
- Dependências de runtime do core DarkFac no produto.
- Reabrir decisões D1–D5 resolvidas pela OA-042.
- Implementar, publicar ou alterar arquivos nesta etapa de planejamento.

## Desenho
Aplicar as skills 02-plan-product-architecture e 19-run-ticket na execução posterior. M1 registra o SHA inicial 23960d95add7595952c8b8ba679af13becc43348; M2 comprova venv, validate, wheel e smoke. Esses registros são históricos, não confirmação do estado remoto atual. O produto permanece em repositório privado separado, Python 3.12+, runtime de biblioteca padrão, página com 'DarkFac pilot' e GET /healthz com status e versão. Usar o descritor existente com setup/validate/build/smoke e build instalado no venv. OA-043 e OA-047 estão abertas: reutilizá-las como dependências, sem duplicação ou simulação de mensagem do owner. Depois do intake real, executar revisão em família diferente, PR, CI e merge no produto; publicar em projeto Dokploy separado de darkfac-core. Vincular SHA mergeado ao artefato implantado e ao smoke. Demonstrar independência instalando o wheel em ambiente limpo fora do checkout DarkFac. Alterações da fábrica passam por python scripts/line_validate.py e pelo portão oficial python core/harness/runner.py --quick; produto passa por seus próprios comandos. Todos os tickets terminam verdes; falta de acesso mantém bloqueado o estágio dependente.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md até docs/evidence/USR-279-M7.md
- .factory/demands/demands.json
- GCamposGit/pilot-hello: src/pilot_hello/
- GCamposGit/pilot-hello: tests/
- GCamposGit/pilot-hello: scripts/smoke.py
- GCamposGit/pilot-hello: .github/workflows/ci.yml
- GCamposGit/pilot-hello: Dockerfile

## Riscos
- OA-043 e OA-047 abertas impedem confirmar acesso de deploy e intake real; não declarar esses marcos concluídos sem prova.
- O plano contém decisões e comandos históricos superados pela OA-042 e pelas evidências M1/M2; atualizar a documentação sem repetir recursos.
- Versão semântica isolada não identifica o SHA implantado; registrar também a proveniência do artefato.
- Busca textual por darkfac produz falso positivo no texto aprovado 'DarkFac pilot'; verificar imports, dependências e chamadas efetivas.
- Arquivar o repositório antes do relatório e da última integração impediria concluir o ciclo.
