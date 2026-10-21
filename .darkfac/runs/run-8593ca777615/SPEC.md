# SPEC

## Objetivo
Concluir M3–M7 do USR-279, provando entrega autônoma do pilot-hello privado, publicado em ambiente separado e executável sem DarkFac.

## Fora de escopo
- Recriar repositório, bootstrap ou ambiente já comprovados em M1/M2.
- Contratar serviços, comprar domínio ou alterar darkfac-core para hospedar o produto.
- Adicionar banco, autenticação, telas ou regras de negócio.
- Reabrir D1–D5, resolvidas pela OA-042.

## Desenho
Reutilizar GCamposGit/pilot-hello e o descritor existente em .factory/projects.json. As evidências locais de M1/M2 registram o SHA inicial 23960d95add7595952c8b8ba679af13becc43348, venv, testes, wheel e smoke aprovados; confirmar o estado atual antes de executar. Produto Python 3.12+, runtime exclusivamente com biblioteca padrão, GET / com 'DarkFac pilot', GET /healthz com status e versão, PORT padrão 8080. Executar setup/validate/build/smoke no venv do produto; validate é o nome canônico do comando de testes. Retomar M3 pela entrada real acompanhada em OA-047 e M5 pela configuração acompanhada em OA-043, sem duplicar ações humanas nem confundir o deploy prévio do Hub com o deploy do produto. Cada ticket termina com validate verde e revisão independente da árvore final; alterações no DarkFac também exigem python scripts/line_validate.py e o portão python core/harness/runner.py --quick. Evidências devem vincular demanda, revisão de outra família, PR, CI, SHA de merge, artefato e deploy. Configuração e execução do piloto ficam em projeto Dokploy separado, sem ampliar o allowlist de deploy do darkfac-core.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md a USR-279-M7.md
- .factory/projects.json, somente se o descritor precisar de ajuste verificado
- GCamposGit/pilot-hello: src/pilot_hello/, tests/, pyproject.toml, Dockerfile, .github/workflows/ci.yml e scripts/smoke.py

## Riscos
- OA-043 e OA-047 são dependências existentes; sua conclusão não pode ser presumida.
- As evidências de M1/M2 são históricas e não comprovam o estado atual do remoto, CI ou infraestrutura.
- O caminho Windows registrado no descritor pode não existir no host executor; resolver checkout sem criar outro projeto.
- Versão semântica isolada não identifica o SHA publicado; exigir vínculo verificável entre commit, imagem e implantação.
- Arquivar o repositório antes da coleta final impediria correções; aplicar D4 somente após validação completa.
