# SPEC

## Objetivo
Retomar M3–M7 do USR-279 e provar entrega autônoma do pilot-hello, com publicação e independência verificadas por SHA.

## Fora de escopo
- Recriar o repositório ou o ambiente comprovados em M1/M2.
- Novas contratações, domínio pago, banco, autenticação ou novas regras de negócio.
- Dependência de runtime do DarkFac no produto.
- Implementação, publicação ou alteração de arquivos nesta etapa de planejamento.

## Desenho
Reutilizar M1/M2 conforme docs/evidence/USR-279-M1.md e USR-279-M2.md: repositório privado GCamposGit/pilot-hello, commit inicial 23960d95add7595952c8b8ba679af13becc43348, venv próprio e comandos setup/validate/build/smoke. OA-042 fixa página 'DarkFac pilot', Python 3.12+, runtime apenas com biblioteca padrão, GET / e /healthz, PORT padrão 8080, wheel com build no venv e retenção arquivada. Executar sequencialmente os cinco tickets restantes, sem duplicar recursos existentes. OA-043 e OA-047 estão abertas no registro consultado; verificar seus efeitos reais antes de liberar os estágios dependentes, sem simular envio do owner. Deploy em projeto Dokploy separado de darkfac-core, pelo domínio existente. Evidências devem relacionar demanda, ticket PH, revisão independente, SHA do PR, CI, SHA do merge, imagem implantada e smoke. Para cada ticket, executar python scripts/line_validate.py; mudanças no núcleo também exigem python core/harness/runner.py --quick. No produto, executar os comandos do próprio descritor, incluindo validate. Implementação futura segue skill 19-run-ticket e preflight de cota.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md a USR-279-M7.md
- .factory/projects.json
- .factory/demands/demands.json
- GCamposGit/pilot-hello: src/pilot_hello/, tests/, pyproject.toml, Dockerfile, .github/workflows/ci.yml

## Riscos
- M1/M2 são evidências históricas locais; conferir estado atual antes de reutilizar seus resultados.
- OA-043 bloqueia acesso/deploy; OA-047 bloqueia a prova de entrada real pelo owner.
- O bootstrap já criou implementação inicial; a retomada deve preservar o trabalho existente e produzir somente alterações necessárias.
- Versão semântica isolada não prova o SHA implantado; registrar também a associação entre imagem, deploy e commit.
- O texto aprovado 'DarkFac pilot' é permitido; buscas de independência devem distinguir esse conteúdo de imports, caminhos e chamadas ao núcleo.
- Arquivar o repositório antes de concluir CI, deploy e provas impediria a continuidade do piloto.
