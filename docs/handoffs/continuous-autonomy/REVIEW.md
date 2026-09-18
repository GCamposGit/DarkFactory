# Revisão adversarial do plano — 18/09/2026

Autor: codex-planner-01a0b4c8, mesmo sujeito que redigiu o pacote.
**Não é revisão independente, EvidenceReceipt, qualificação de modelo ou aprovação de PR.**
Escopo: composição, fronteiras de autoridade, fontes do roadmap, DAG, contratos de handoff,
rotas e desenho do ensaio. Não executamos fábrica cloud, deploy, recuperação ou janela24h.

| Contraexemplo que poderia aprovar incorretamente | Correção incorporada / oráculo |
| --- | --- |
| Inserir HF-05-02 na tabela HF e parser lê HF-05 | Subtickets no JSON já consumido; verificar projeção dos 33 IDs completos |
| Arquivo hf-05-02-report.md vazio marca pai/filho concluído | Planejamento fora do glob; sem evidence_refs; HF-13-01 corrige inferência e prefixo |
| Usar 83e5298 como se main remoto ou host tivessem os módulos | Proveniência local/remota separada; HF-26-03 reconcilia sete commits sem publicação incidental |
| Matar processo entre gravar JSON e criar job | Canonical accept transacional e JSON apenas projeção; replay + crash points |
| Duas filas concedem mesmo slot ou custo | Política HF-23 pura, concessão só na transação da lease do store |
| Commit DB e enqueue DBOS tratados como uma transação | Outbox + workflow ID determinístico + reconcile; não prometer atomicidade distribuída |
| lease antiga confirma enquanto nova executa | Fencing crescente em heartbeat/result/effect; stale owner rejeitado |
| Run WAITING_HUMAN suspende todos os ramos | Dependência por job/descendente; resumo do run derivado |
| Cada módulo implementado mas consumer não iniciado | Processo permanente; health exige progresso; caso negativo desliga consumer com HTTP200 |
| Worker só retorna nome de modelo e texto | Binding exige edição, ferramenta, teste e resume/cancel no host/conta observados |
| Fallback local enfraquece qualidade de arquitetura | Piso por papel; ausência de rota vira espera diagnosticada, não escolha silenciosa |
| Webhook2xx ou booleans do chamador provam deploy | Operação externa + término + digest de bytes instalado + jornada persistente independente |
| Release binding só cobre Dokploy e aplica a ATRIUM | Inventariar FTP/local_service; contratos por target e projeto antes de liberação econômica |
| Pack não lido ou retry exploratório trava carteira | Pack saída sem ack; pesquisa exploratória separada; bloqueio só por contrato invalidado |
| Aprendiz gera eventos sobre si e expande escopo infinito | Causation/dedupe, tipos internos excluídos, mesmo hash não replaneja sem novo fato |
| Candidato muda holdout e se aprova | Holdout/política congelados por avaliação; avaliador de identidade distinta |
| Controlador do ensaio invoca fases e chama isso autonomia | Só input/resposta/injeção de falha autorizada; observador verifica origem de cada job |
| Handoff JSON high e hash confundidos com atestado | approval_reference unresolved; gate sem VerificationContext deve reprovar; admissão do supervisor |
| Critério exige produção antes de implementar primeiro módulo | Gates por etapa; HF-26-01 local puro, sem credencial; rollout no fim do DAG |
| Dois gates paralelos distorcem benchmark500ms | Falha inicial preservada; repetir focal isolado e suites sequenciais sem alterar limiar |

Veredito desta autoanálise: contratos corrigidos no escopo acima; **revisão independente
permanece pendente**, portanto não emitir delivered nem merge aprovado por esta revisão.
Sem novo Grill: lacunas remanescentes são técnicas e têm binding high explícito.

Limitação do primeiro handoff: a especificação está pronta para implementação, mas o
ambiente desta tarefa não fornece atestado verificável de variante/esforço do modelo e
não há contexto de aprovação confiável. Não fabricar PlanApproval para fazer gate verde.
O supervisor qualificado deve admitir o contrato; é tarefa técnica, não pergunta ao owner.

Riscos abertos com destino: API DBOS/locks/schema (HF-05-02); rotas tool-calling (HF-07-01);
conversões reais por handler (HF-09-01); API/status/digest e rollback por target (HF-12-01);
capacidade/secret refs instaladas (HF-03-07). Econômico não começa essas folhas antes do binding.
