# SPEC

## Objetivo
Retomar o USR-279 a partir de M1/M2 (ja concluidos com evidencia) e entregar o marco M3 no que depende da fabrica: tornar a execucao de tickets do projeto pilot-hello segura e previsivel em qualquer host (fail-closed de acesso ao repositorio privado + afinidade de execucao), automatizar a prova de independencia do produto (ferramenta usada em M6) e preparar o descritor/guardas de deploy para M5, sem recriar repositorio, ambiente ou decisoes de produto ja tomadas na OA-042.

## Fora de escopo
- Nao contratar servicos novos nem criar novo dominio pago.
- Nao inventar decisoes de negocio do produto (D1-D5 ja respondidas na OA-042).
- Nao recriar o repositorio GCamposGit/pilot-hello, o checkout local nem o venv do produto (M1/M2 concluidos; bootstrap e idempotente).
- Nao enviar a demanda pelo Telegram em nome do owner (M3 criterio 1 segue na OA-047) nem criar o projeto/webhook Dokploy em lugar do owner (OA-043).
- Nao implementar codigo do produto pilot-hello neste slice (isso e M4, pela propria linha, no repositorio privado).

## Desenho
Tudo neste slice e codigo da fabrica (repositorio DarkFac), validado pelo portao do projeto darkfac (python scripts/line_validate.py).

1) Acesso ao repositorio privado por host. Verificacao real feita no planejamento: neste worker de nuvem o GITHUB_TOKEN enxerga GCamposGit/DarkFactory (public) mas responde 404 para GCamposGit/pilot-hello; logo um job do pilot-hello reivindicado aqui quebraria tarde, com stderr cru de 'git clone', dentro de core/projects/ticket_execution.project_checkout. A correcao e dupla: (a) pre-flight explicito de alcance do remoto (git ls-remote --exit-code) antes de clonar/usar checkout, com ProjectExecutionError estruturado (cause project_repo_unreachable + remediacao citando host e credencial); (b) exec_affinity no descritor do pilot-hello, que core/line/bindings.required_caps ja propaga para required_capabilities dos estagios, de modo que somente hosts que publicam a capability (os do owner, onde M1/M2 rodaram) reivindiquem jobs do piloto. A capability precisa ser publicada em DARKFAC_WORKER_CAPS dos hosts do owner: acao humana, registrada como OA no mesmo turno.

2) Prova de independencia (ferramenta de M6). Novo script determinista scripts/pilot_independence_check.py que clona o repositorio do produto em diretorio temporario fora do checkout DarkFac, executa environment.setup e os comandos declarados (setup/validate/build/smoke) reusando core.projects.environment.setup_environment/run_in_environment (mesmo caminho do M2, que ja remove PYTHONPATH/DARKFAC_*/VIRTUAL_ENV do core e mascara segredos), varre codigo executavel por 'import core'/'from core'/caminho do core/'darkfac' (ignorando documentacao e o texto de produto 'DarkFac pilot' aprovado em D2) e emite relatorio JSON com SHA do clone, comandos, chaves de ambiente observadas e achados da varredura. Execucao real roda no host com acesso; os testes injetam um produto sintetico (repo git temporario) e substituem a camada de ambiente, para nao criar venv na suite.

3) Preparacao do M5. O caminho de deploy pos-merge de projeto externo ja existe (run_ticket._release_external_project -> core.line.stage_release.ReleaseStageHandler) e so dispara quando project.deploy esta preenchido; hoje pilot-hello tem deploy=null e smoke=[]. Em vez de inventar o service id do Dokploy (so o owner cria a aplicacao, OA-043), este slice entrega: guarda no registry contra reaproveitar o service_name/alvo de deploy de outro projeto (impede o piloto apontar para o darkfac-core), descritor-modelo versionado para merge via 'core.projects.cli register --descriptor-file' (CLI ja faz merge parcial) e passos tela a tela na OA-043 com o comando absoluto de registro.

## Arquivos a tocar
- core/projects/ticket_execution.py
- core/projects/registry.py
- .factory/projects.json
- scripts/pilot_independence_check.py
- tests/test_project_ticket_execution.py
- tests/test_pilot_independence_check.py
- tests/test_project_registry_cli.py
- docs/plans/USR-279-greenfield-pilot.md
- docs/plans/USR-279-pilot-deploy-descriptor.json
- docs/evidence/USR-279-M3.md
- .factory/owner_actions/owner_actions.json

## Riscos
- O host de execucao deste slice nao ve o repositorio privado do piloto (404 com o token do worker de nuvem): nenhuma verificacao end-to-end contra o pilot-hello real pode ser feita aqui; por isso os testes usam produto sintetico e a execucao real fica pinada aos hosts do owner via exec_affinity.
- exec_affinity com capability inexistente nos hosts starva os jobs do piloto em vez de falhar: a capability tem de ser publicada em DARKFAC_WORKER_CAPS (OA) e a capability escolhida precisa constar da OA e da evidencia.
- Pre-flight de remoto adiciona chamada de rede por execucao de projeto externo: precisa de timeout curto e nao pode rodar para projetos sem repo_url (ex. darkfac no registry), sob pena de regressao em testes e em tickets internos.
- Guarda de service_name duplicado no registry pode rejeitar entradas legitimas ja existentes (darkfac e darkfac-canary): a regra deve comparar apenas alvos efetivos de deploy e preservar o registry atual sem migracao manual.
- M3 criterio 1 (mensagem real do owner) e M5 (projeto/webhook Dokploy) permanecem dependentes de OA-047 e OA-043; este slice reduz risco mas nao conclui esses marcos.
