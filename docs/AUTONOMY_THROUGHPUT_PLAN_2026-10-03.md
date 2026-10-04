# Autonomia e throughput — ondas de evolução

Versão 1.0 · 03/10/2026 · origem: decisão explícita do owner nesta sessão.

## Objetivo e prioridade

A fábrica deve receber a intenção do owner, transformar demandas em entregas de
software de ponta a ponta, retomar falhas recuperáveis e expor o trabalho em
tempo real. O owner não deve precisar descobrir tickets parados nem solicitar
manualmente correções e aprendizados. Não há cliente pagante hoje; governança
enterprise e controles comerciais ficam depois da autonomia e da qualidade.

Este plano complementa HF-27 e HF-28. Reutiliza tickets já abertos; não reabre
trabalho concluído. Nenhum status `completed` ou marco `delivered` será inferido
apenas de testes sintéticos, PR ou webhook disparado.

## Baseline em 03/10/2026

- `origin/main` em `fcc9a1e`; o checkout compartilhado tem alterações de outra
  sessão em `core/infra/node_sync.py`, `scripts/dokploy_redeploy.py` e testes.
  Esta onda usa worktree própria e não toca esses arquivos.
- HF-27 permanece `validating`; V1–V4 ainda pedem evidência operacional.
- O dogfood seleciona apenas itens `planned` e `line-ok` do manifesto de
  roadmap. O backlog executável em `demands.json` não é consumido (USR-92).
- O painel de tarefas não atualiza automaticamente e não mostra a idade da
  última mudança de cada job.
- A revisão pode aprovar um parecer bloqueante após esgotar rodadas; validação
  sem comando executado pode parecer verde; o canário HTTP procura apenas uma
  data no corpo; a CI dispara deploy sem provar convergência.
- A suíte oficial deve rodar no Notebook nesta sessão com
  `python core/harness/runner.py --quick --local --no-cache`. O Desktop está
  ocupado e não será usado como worker de testes.

## Onda atual — destravar, recuperar e mostrar progresso

| Fatia | Contrato observável | Tickets | Superfície principal |
| --- | --- | --- | --- |
| Consumo do backlog | `planned` + `line-ok` em `demands.json` entra na linha com idempotência; sem tag ou com caminho protegido não entra. O gate de canário permanece explícito. | USR-92 | `core/line/dogfood.py` |
| Retomada autônoma | Esperas técnicas/temporais têm próximo evento e nova tentativa; `waiting_human` só permanece para decisão ou ação humana real. Esgotamento de wall-clock não estaciona o run para sempre. Após rollback, o contexto ainda permite corrigir a regressão. | USR-99, USR-105, USR-106, USR-123, USR-134 | `core/line`, `core/workflow` |
| Progresso visível | O painel atualiza enquanto visível, mostra a idade de cada job, data do último refresh e falha de fonte sem apagar dados anteriores. Status do backlog e canário são vinculados à linha em fatia posterior. | USR-128, USR-93 | `hub/frontend/tasks.js`, depois API de status |
| Qualidade sem falso verde | Parecer `changes_required` nunca vira aprovação por limite de rodadas. Sem comando/prova mínima de validação a entrega não avança. | USR-129, USR-130, USR-131 | `stage_review.py`, `stage_build.py`, `stage_integration.py`, `stage_release.py` |
| Prova de operação | Canário comprova data **e SHA** do artefato implantado; deploy só fecha com convergência e jornada observada; V1–V4 têm evidência consultável. | USR-73, USR-93, USR-132 | `canary.py`, release/acceptance |

### Ordem de integração da onda atual

1. Entregar fatias disjuntas de dogfood e painel, mantendo as alterações da
   sessão Antigravity intocadas.
2. Eliminar os dois caminhos de falso verde antes de ampliar a alimentação
   automática do backlog.
3. Fechar retomada de jobs parados e a verificação operacional, aproveitando
   os tickets existentes e evitando implementar duas soluções para o mesmo
   estado de espera.
4. Executar o portão único no Notebook, CI do PR e integração somente com
   prova de resultado. Cada fatia registra ticket, candidato e evidência.

O dogfood preserva o limiar de sete dias verdes do canário já aprovado em
HF-27. Alterá-lo exigiria decisão explícita do owner. A implementação do
consumidor pode ser concluída antes de o gate operacional abrir.

## Ondas seguintes — não iniciar nesta entrega

| Onda | Resultado desejado | Reúso e condição |
| --- | --- | --- |
| Avaliação de sistemas completos | Demandas reservadas de produtos distintos medem jornada, defeitos, custo, duração, recuperação e intervenção humana; mudanças de modelo e prompt são comparadas contra baseline. | USR-133; expandir `evals/` após o caminho de entrega produzir recibos confiáveis. |
| Aprendizado fechado | Incidente, revisão e deploy geram lição candidata; uma avaliação independente promove ou rejeita a lição, e o planning seguinte consome a versão aprovada. | USR-91 e skill de aprendizado; não promover regras pelo relato de um único run. |
| Adoção e autorreparo multiprojeto | O projeto novo comprova setup, testes, preview, deploy e rollback; sintomas de produção iniciam diagnóstico e correção limitados. | V4 de HF-27, catálogo de projetos e observação operacional. |
| Governança proporcional | Isolamento de credenciais por etapa, políticas comerciais e controles enterprise. | Depois de demonstrada autonomia/qualidade e conforme apareçam clientes e risco reais. |

## Critérios de aceite do pacote

1. Uma demanda elegível fica visível desde `planned`, é submetida uma vez à
   linha quando o gate abrir e avança sem um novo prompt do owner.
2. Um job pronto parado, uma espera técnica sem próxima tentativa e um erro
   de agente repetido são detectáveis e recebem recuperação ou causa terminal.
3. O owner vê estágio, responsável, idade do progresso, última atualização e
   motivo da espera no DarkHub sem recarregar a página.
4. Nenhum PR é integrado porque revisão bloqueante foi convertida em
   aprovação, porque zero testes executaram ou porque só um webhook respondeu.
5. Canário e aceite V1–V4 vinculam o resultado ao SHA executado no destino.
6. O portão oficial roda integralmente no Notebook; relatório informa testes
   executados, CI, PR, merge, deploy e limitações operacionais restantes.

## Referências externas usadas na decisão

- Anthropic, *Building effective agents* e *Demystifying evals for AI agents*:
  simplicidade de composição, avaliações de trajetórias e resultados.
- OpenAI, *A practical guide to building AI agents*: baseline de qualidade
  antes de otimização de custo e limites claros de execução.
- SWE-bench e OpenHands Benchmarks: tarefas reais em ambientes reproduzíveis.
- DORA e Google SRE: entrega e confiabilidade medidas pelo resultado em
  produção, não pela quantidade de etapas ou PRs.

URLs e comparação detalhada constam da avaliação apresentada ao owner em
30/09/2026; estas fontes orientam a decisão, mas não substituem testes da
própria Dark Factory.
