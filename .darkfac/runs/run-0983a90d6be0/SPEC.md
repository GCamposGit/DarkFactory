# SPEC

## Objetivo
Concluir M3–M7 do USR-279, provando entrega autônoma do pilot-hello com evidências reais por SHA e execução independente do DarkFac.

## Fora de escopo
- Recriar repositório, registro ou ambiente já entregues em M1/M2.
- Contratar serviços, comprar domínio ou inventar regras de negócio.
- Adicionar banco, autenticação ou dependência de runtime do DarkFac.
- Implementar ou publicar durante esta etapa de planejamento.

## Desenho
Reutilizar OA-042 e as evidências locais docs/evidence/USR-279-M1.md e USR-279-M2.md: repositório privado GCamposGit/pilot-hello, commit inicial 23960d95add7595952c8b8ba679af13becc43348, pacote Python e venv já existentes. Verificar o estado remoto antes de executar os marcos restantes. O produto usa Python 3.12+, biblioteca padrão no runtime, página com 'DarkFac pilot', GET /healthz com status e versão e PORT padrão 8080. Executar sequencialmente T1–T5, com até três critérios por marco. OA-047 condiciona a prova de entrada real pelo Telegram; OA-043 condiciona acessos e deploy no projeto Dokploy separado, sem recriar ações humanas. No produto, usar os comandos setup/validate/build/smoke do descritor, com wheel obrigatório e CI próprio. Alterações no DarkFac devem passar por python scripts/line_validate.py e pelo portão oficial python core/harness/runner.py --quick em candidato isolado. Cada ticket encerra verde; dependência indisponível mantém o ticket pendente. Evidências incluem projeto, ticket, SHA candidato e mergeado, revisão independente, PR, CI, implantação e horários. Arquivar o repositório somente após a prova e o relatório.

## Arquivos a tocar
- docs/plans/USR-279-greenfield-pilot.md
- docs/evidence/USR-279-M3.md até docs/evidence/USR-279-M7.md
- .factory/demands/demands.json
- GCamposGit/pilot-hello: src/pilot_hello/, tests/, pyproject.toml, .github/workflows/ci.yml e Dockerfile, somente conforme lacunas verificadas

## Riscos
- M1/M2 têm evidências históricas locais; isso não comprova o estado remoto atual nem CI ou publicação.
- OA-043 e OA-047 podem continuar pendentes; não substituir a entrada humana real por evento sintético.
- A versão semver isolada não identifica o SHA implantado; é necessário correlacionar artefato e registro de deploy.
- A busca por dependências proibidas precisa permitir o texto aprovado 'DarkFac pilot'.
- Arquivamento prematuro pode impedir correções, CI ou conclusão da implantação.
