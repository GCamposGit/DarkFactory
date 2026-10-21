# SPEC

## Objetivo
Concluir M3–M7 do USR-279 e provar entrega autônoma do piloto privado GCamposGit/pilot-hello, com publicação no Dokploy existente e funcionamento independente do DarkFac.

## Fora de escopo
- Recriar repositório, bootstrap ou ambiente já comprovados em M1/M2.
- Novas contratações, domínio pago, banco de dados, autenticação ou novas funcionalidades de negócio.
- Alterar o núcleo compartilhado sem defeito demonstrado e ticket próprio.
- Implementar ou publicar durante esta etapa de planejamento.

## Desenho
Reutilizar docs/evidence/USR-279-M1.md e M2.md: repositório privado criado no SHA 23960d95add7595952c8b8ba679af13becc43348; ambiente venv, testes, wheel e smoke registrados. Reconferir a baseline antes da execução, sem considerar essas evidências como prova de revisão, CI ou deploy. Produto Python 3.12+, runtime somente com biblioteca padrão, página GET / com 'DarkFac pilot', GET /healthz com status e versão, execução por python -m pilot_hello e PORT padrão 8080. OA-042 resolve D1–D5; OA-043 e OA-047 permanecem abertas e bloqueiam respectivamente publicação e comprovação do intake real. Executar M3–M7 sequencialmente, até três critérios por marco. O descritor do produto usa validate, não test; a validação do DarkFac usa python scripts/line_validate.py e alterações no core exigem também o portão oficial python core/harness/runner.py --quick. Revisão independente em outra família, CI, merge, artefato e deploy devem formar uma cadeia verificável de SHAs. Publicação em projeto Dokploy separado de darkfac-core, sem ampliar o allowlist do redeploy do core. Manter o repositório arquivado após concluir todas as verificações e entregas.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md a docs/evidence/USR-279-M7.md
- .factory/demands/demands.json
- GCamposGit/pilot-hello: src/pilot_hello/, tests/, scripts/smoke.py, pyproject.toml, Dockerfile e .github/workflows/ci.yml, somente conforme lacunas verificadas

## Riscos
- OA-047 aberta: não substituir a mensagem real do owner por intake sintético.
- OA-043 aberta: acesso privado, aplicação e webhook precisam ser verificados antes da publicação; reutilizar a ação existente.
- M1 já contém implementação: produzir alterações artificiais ou recriar recursos não comprova a passagem real pela linha.
- Versão semântica isolada não identifica o deploy: vincular também artefato, execução do Dokploy e SHA de merge.
- O texto autorizado 'DarkFac pilot' não constitui dependência do core; buscas devem distinguir texto de imports e chamadas executáveis.
- Arquivar antecipadamente impede PRs e correções; aplicar retenção somente após concluir a entrega.
