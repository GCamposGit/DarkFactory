# SPEC

## Objetivo
Concluir M3–M7 do USR-279, provando entrega autônoma do pilot-hello privado até deploy e execução independente, reutilizando M1 e M2.

## Fora de escopo
- Recriar repositório, registry ou ambiente já comprovados em M1 e M2.
- Contratar serviços, comprar domínio ou alterar darkfac-core para hospedar o produto.
- Adicionar banco, autenticação, novas telas ou decisões de negócio.
- Implementar ou publicar durante esta etapa de planejamento.

## Desenho
Reutilizar GCamposGit/pilot-hello, página com texto 'DarkFac pilot', servidor Python 3.12+ com biblioteca padrão, GET / e GET /healthz, PORT padrão 8080. As evidências locais M1 e M2 registram bootstrap idempotente, três testes, wheel e isolamento do venv; não comprovam CI remoto nem entrega ponta a ponta. Executar M3–M7 em sequência com tickets PH vinculados à demanda real, revisão de família diferente, PR, CI por SHA, merge e deploy em projeto Dokploy separado. Reutilizar OA-042; verificar OA-047 antes de M3 e OA-043 antes de M5, sem repetir perguntas. Usar os comandos setup/validate/build/smoke do descritor do produto, com build no venv. Mudanças no core exigem python scripts/line_validate.py e o portão oficial python core/harness/runner.py --quick. Cada ticket termina com validate verde; indisponibilidade externa mantém o marco pendente, sem sucesso simulado.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md até docs/evidence/USR-279-M7.md
- .factory/demands/demands.json
- GCamposGit/pilot-hello: src/pilot_hello/
- GCamposGit/pilot-hello: tests/
- GCamposGit/pilot-hello: .github/workflows/ci.yml
- GCamposGit/pilot-hello: Dockerfile
- GCamposGit/pilot-hello: scripts/smoke.py

## Riscos
- OA-047 pode bloquear a prova de entrada real; uma demanda sintética não substitui mensagem do owner.
- OA-043 pode bloquear acesso ao repositório privado, webhook ou deploy.
- O produto já contém implementação inicial; repetir bootstrap ou inventar mudanças apenas para produzir PR invalidaria a prova.
- Versão semver isolada não identifica o SHA implantado; a cadeia entre artefato, deploy e smoke deve ser verificável.
- Arquivamento deve ocorrer após todas as evidências e publicações necessárias.
- A busca de dependências deve permitir o texto aprovado 'DarkFac pilot' e rejeitar imports, caminhos e serviços do core.
