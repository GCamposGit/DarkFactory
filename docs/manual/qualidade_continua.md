<!-- covers: core/line/quality_cycle.py, core/demands/queue_store.py, core/demands/store.py -->
# Qualidade contínua

## Estado

O ciclo recorrente de qualidade ainda não está ativo. O ticket T1 entrega uma base necessária:
o Hub, o domínio de demandas e o `BacklogFeeder` consultam o control store canônico da linha.
Quando Postgres está configurado, a leitura do Hub não depende do snapshot `demands.json`.
Indisponibilidade do store é erro estruturado HTTP 503; somente um identificador ausente retorna
404. O fallback SQLite e o uso file-only por `DARKFAC_DEMANDS_PATH` continuam disponíveis para
desenvolvimento local.

## Contrato previsto para o ciclo

O ciclo será chamado pelo `CloudCoordinator` existente e usará o control store existente. Não
criará outro agendador, catálogo, roteador ou fila. Cada rodada deverá persistir sua cobertura,
custo, motivo e resultado, distinguindo `success_no_findings`, `partial`, `failed`,
`waiting_resource` e `not_applicable`. Cadência, vencimentos e eventos deverão ser reconciliados
com chaves de vencimento estáveis e leases com fencing para consolidar atrasos e impedir execução
concorrente duplicada.

Coletores determinísticos serão responsáveis por evidências verificáveis. Seus fingerprints
incluirão código, dependências, regras, versão do coletor e revisão externa; evidência válida será
reutilizada. Inferência por LLM ficará limitada a ambiguidades comprovadas, com contexto e custo
delimitados. Um achado só entrará na fila atual após qualificação e deduplicação atômica por chave
estável, seguindo o fluxo normal de feeder, roteamento, revisão, CI, deploy e rechecagem.

Cobertura parcial, falha ou não aplicabilidade não poderá ser reportada como saúde completa.
Coletores de documentos, advisories e telemetria deverão preservar `partial` ou
`not_applicable` quando a evidência não sustentar uma conclusão segura.
