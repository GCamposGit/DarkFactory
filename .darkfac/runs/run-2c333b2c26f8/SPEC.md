# SPEC

## Objetivo
Fechar o marco M7 do piloto greenfield USR-279 com um relatorio auditavel ao owner: linha do tempo M1-M6 com status confirmado ou pendente, SHAs, PR, CI, deploy, smoke, prova de independencia e contagem de intervencoes manuais; cada falha do percurso vinculada a commit/PR ou ticket formal; decisao D4 (manter arquivado) registrada como acao do owner; e os criterios 2 e 3 do USR-279 marcados apenas com a evidencia real correspondente, com pendencia explicita quando a evidencia nao existir.

## Fora de escopo
- Executar M3, M4, M5 ou M6 (demanda pelo Telegram, PR/CI do produto, deploy e smoke no Dokploy, prova de independencia nova).
- Criar ou configurar projeto Dokploy, webhook, token ou escopo do gh (coberto por OA-043) e enviar a demanda pelo Telegram (coberto por OA-047).
- Arquivar ou remover o repositorio pilot-hello: a execucao de D4 e acao do owner na conta dele.
- Reabrir D1-D5 ou inventar nova decisao de produto/negocio.
- Declarar o piloto concluido ou marcar criterio como atendido sem evidencia verificavel.
- Alterar o contrato de bootstrap (USR-274), de ambiente (USR-275) ou de roteamento Telegram (USR-278).

## Desenho
Tres passos encadeados sobre os registros que ja existem (docs/plans/USR-279-greenfield-pilot.md, docs/evidence/USR-279-M1.md, docs/evidence/USR-279-M2.md, .factory/demands/demands.json, .factory/owner_actions/owner_actions.json, git log e, quando alcancavel, o repo pilot-hello). Primeiro um inventario de falhas e lacunas em docs/evidence/USR-279-M7-falhas.md, onde cada linha carrega evidencia e um vinculo de resolucao obrigatorio (commit, PR, ticket USR-/PH- ou OA-), abrindo ticket formal na fila para o que ainda estiver aberto (CI do produto nao verificada no GitHub Actions, wheel nao provado no clone, marcos M3-M6 bloqueados). Depois o relatorio docs/evidence/USR-279-M7.md com a linha do tempo M1-M6 (uma linha por marco, status confirmado ou pendente), secoes de SHAs/PR/CI, deploy e smoke, prova de independencia e contagem de intervencoes manuais (realizadas versus pendentes), acompanhado de um verificador novo core/evidence/greenfield_pilot.py (contratos pydantic v2, funcoes puras parse_report e verify_report) que recusa status desconhecido, confirmado sem referencia verificavel, pendente sem bloqueador e qualquer declaracao de piloto concluido com marco pendente. Por fim o registro da acao do owner para D4 via scripts/owner_action.py add e a reescrita do delivery_evidence do USR-279 criterio por criterio via core.demands, com o verificador estendido para cruzar o que esta marcado no ticket com o que o relatorio confirma. As escritas em demands.json e owner_actions.json usam somente as CLIs, porque outras sessoes escrevem nesses arquivos.

## Arquivos a tocar
- docs/evidence/USR-279-M7-falhas.md
- docs/evidence/USR-279-M7.md
- core/evidence/__init__.py
- core/evidence/greenfield_pilot.py
- tests/test_usr279_m7_failures.py
- tests/test_usr279_m7_report.py
- tests/test_usr279_m7_criteria.py
- docs/plans/USR-279-greenfield-pilot.md
- .factory/demands/demands.json
- .factory/owner_actions/owner_actions.json

## Riscos
- Declarar o piloto concluido ou marcar os criterios 2 e 3 sem evidencia de M3-M6: mitigado pelo verificador e pelos testes, que exigem referencia confirmada por marco e por criterio.
- O checkout do produto (C:\dev\pilot-hello), o gh autenticado e o Dokploy podem nao estar alcancaveis no host que executa o ticket; nesse caso o item fica pendente com o bloqueador citado, nunca presumido como atendido.
- demands.json e owner_actions.json sao escritos concorrentemente por outras sessoes: edicao manual geraria conflito de merge; usar apenas core.demands e scripts/owner_action.py.
- Criar OA duplicada para D4 (OA-042 ja registrou a decisao): verificar o backlog antes de adicionar a acao de retencao.
- Teste baseado em parse de markdown pode ficar fragil: manter o contrato do relatorio minimo (cabecalhos e colunas obrigatorias) e validar o resto por referencia de ID existente.
