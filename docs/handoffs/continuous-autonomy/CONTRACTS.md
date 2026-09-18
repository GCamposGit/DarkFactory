# Contratos comuns de composição — v1.0

Normativo para os handoffs do [índice](INDEX.md). Todas as APIs desta seção são propostas
novas, exceto serviços explicitamente identificados como existentes. Nenhum novo comando
CLI de operação é considerado disponível antes de sua unidade de implementação.

## Tipos e fronteiras

`core/workflow/control_contracts.py` (NOVO, HF-05-02 especifica; HF-05-03 implementa):

- `IntakeCommand(project_id, channel, external_id, payload, mode, policy_ref)`;
  mode literal `autonomous|documentary`. Payload contém título, problema, jornada,
  non-goals e critérios; canonical JSON UTF-8, sorted keys, separators comma/colon,
  sem NaN. Hash SHA-256 dos bytes; não ordenar arrays nem normalizar texto de intenção.
- `IntakeReceipt(demand_id, demand_version, run_id, initial_job_id, mode, committed_at)`.
  IDs não vazios no modo autônomo. Os três IDs de execução são null no documental.
- `JobKey(run_id, ticket_id, plan_version, stage, iteration)`; iteration >=0.
  Chaves de banco são tuplas, não strings concatenadas ambiguamente.
- `Claim(job_key, lease_id, owner, fencing_token, expires_at, reservation_id, route_ref)`.
  Fencing monotônico por job, nunca reiniciado após release. Token não substitui identidade.
- `StageContext(claim, plan_ref, plan_digest, candidate_digest, config_version,
  environment_ref, identity, route_ref, memory_version, input_refs)`.
- `StageResult(outcome, output_refs, evidence_refs, operation_refs, actual_cost,
  cause_code)`; outcome `success|retry|replan|waiting_dependency|cancelled|failed`.
  Success sem output requerido é inválido. Nenhum campo `approved=true` vale como evidência.
- `HandlerDescriptor(stage, version, input_schema_ref, output_schema_ref, role,
  required_capabilities, timeout_seconds, conflict_scope)`.
- `ExternalOperation(operation_key, request_digest, provider, external_id,
  status, observed_at)`; status `prepared|sent|unknown|succeeded|failed`.
- `ReconcilePage(cursor, visited_projects, repaired_keys, next_cursor, cycle_id)`.

`ControlStore` (Protocol NOVO) expõe `accept(command, now)`, `claim(worker, capabilities, now)`,
`heartbeat(claim, now)`, `finish(claim, result, now)`, `materialize(event, now)`,
`reconcile(now, cursor, limit=100)`, `get_operation(key)`, `record_operation(operation, claim)`.
Assinaturas finais de adaptação SQLite/DBOS são saída vinculante de HF-05-02; não cabe ao
econômico decidir schema, semântica de locking ou API externa. Contratos acima fixam
comportamento; binding deve fornecer tipos Python completos e casos executáveis por backend.

Exemplo válido: mesma demanda `darkfac/hub/ext-7` duas vezes, payload igual → mesmo recibo,
um Grill; `finish` com owner atual/fencing 2 e recibo vigente → um sucessor e uma observação.
Inválidos: ext-7 com outro texto → `IdempotencyConflict`; finish fencing 1 → `StaleLease`;
projeto alias não resolvido → `UnknownProject`; handler sem versão → `MissingBinding`;
receipt de outro digest → `EvidenceMismatch`. Nenhum erro parcial confirma execução.

## Registry de handlers e materialização

Handler factory: `build_handlers(bindings, services) -> Mapping[tuple[str,str], StageHandler]`.
`StageHandler.handle(context: StageContext) -> StageResult`. Binding validado antes de claim.
Nunca carregar código arbitrário por nome enviado na demanda. Registro allowlist versionado;
versão fixada por run, allowlist de ferramentas por papel. Dados de pesquisa são dados,
não instruções para aumentar permissões. Adaptadores recebem referências de segredo resolvidas
na fronteira de I/O, não valores em contexto/exportação.

| Stage | Entrada | Serviço existente a ligar | Saída / próximo trabalho |
| --- | --- | --- | --- |
| grill | Demand revision | IntegratedIntakeService/GrillEngine | GrillRecord → planning ou dependência seletiva |
| planning | Grill+memória+contratos | specifier + WorkflowHandoff + ReadinessGate | plano aprovado e filhos → environment/development |
| research | questão versionada | core/research e PersistentMemoryService.record_research | ledger com fontes → planning dependente |
| environment | EnvironmentManifest | probes cloud/infra/reconciliation | EnvironmentEvidence ou ManualDependency → jobs afetados |
| development | handoff vigente | AgentExecutor.start/resume, ImplementationCycleService.start_development | candidato SHA/digest → validation |
| validation | candidato | ImplementationCycleService.run_validation + harness | logs vinculados → independent_review ou correção |
| independent_review | candidato e oráculos | conduct_independent_review | EvidenceReceipt de sujeito distinto → integration |
| integration | candidato aprovado | delivery_executor + GitHub snapshot | PR/merge/base/head observados → build |
| build/deploy/journey | merge+manifesto de alvo | release_pipeline + DokployDeployClient + target adapter | bytes/digest/operação/jornada → próxima etapa/gate |
| memory_observation | resultado/falha | PersistentMemoryService.register_candidate | hipótese deduplicada → learning_eval |
| learning_eval/promotion | hipótese+holdout | FactoryEvolutionEngine | recibo distinto → promoção versionada/aplicação/rollback |
| catalog_refresh | timer 24 h/boot atrasado | benchmarks/router qualification | catálogo qualificado para novos jobs |

Para cada linha, HF-09-01 (produção) e HF-12-01 (release) devem publicar binding com
argumentos e conversões reais. Enquanto binding estiver ausente, leaf fica
`needs_architecture_binding`, nunca entregue a econômico com TODO de arquitetura.

Sucessor produtivo e aprendizado têm dedupe independente. Uma observação de aprendizado
não gera observação idêntica recursivamente: excluir eventos internos de bookkeeping,
registrar `causation_id` e rejeitar ciclo no grafo de causalidade. Replanejamento exige nova
evidência ou decisão; mesma causa+input hash+versão não produz infinitos planos.

## Ambiente e autoridade

Cada unidade tem EnvironmentManifest herdado e complemento local. Perfil `local`:
Python 3.12+, pytest, git; filesystem da worktree exclusiva, loopback apenas nos testes;
sem segredo ou efeito remoto. Target não é comprovado por esse perfil.
Perfil `cloud-binding`: leitura de versões e configuração sanitizada no host autorizado;
DSN apenas SecretReference para variável `DARKFAC_HF02_DATABASE_URL`; nenhum valor em args/logs.
Perfil `executor-binding`: model ID/effort aceito, conta/host, versão SDK/CLI, tool calling,
resume/cancel, quota/reset, preço e limite observados; aliases comerciais não comprovam rota.
Perfil `release-binding`: target real por projeto, URLs sem tokens, secret refs GitHub/
Dokploy/FTP, permissões mínimas, deploy ID e observador. Não há URLs reais de operação
inventadas neste documento. HF-03-07 resolve endpoints, portas, volumes e config digest.

Toda unidade: identity subject/role/host/account_ref sanitizada; candidato SHA, plan_digest,
config_version, environment_ref, rota e observed_at UTC. SecretReference indica provider,
locator e variable_name sem valor. Mudança relevante invalida apenas evidência dependente.
`PlanApproval` vem do supervisor qualificado; `EvidenceReceipt` de revisão vem de sujeito
distinto do implementador. Candidato não cria seu VerificationContext confiável.
Hash do plano é SHA-256 do manifesto canônico de arquivos relativos+bytes do contrato;
aprovação é externa ao manifesto para evitar ciclo. Hash não autentica autor.

## Execução, recuperação e ownership

Preflight: branch não detached, baseline compatível, paths livres, worktree limpa,
lease do executor e rota atual. Revalidar hashes relevantes após predecessores; se mudou
API/critério, rebind; mudança alheia não invalida portfólio inteiro.
Paths permitidos por unidade são exatos; todo o restante, inclusive AGENTS/MISSION/
FACTORY_RULES, é somente leitura. Artefatos auxiliares permitidos: relatório e logs exclusivos
do ticket, jamais critérios/holdout/skills não declarados. Não alterar verificador para passar.

Heartbeat 10 s, lease 45 s, timeout de unidade 30 min por default (binding/probe 5 min;
release 15 min; aceitação 24 h com heartbeat). Esses defaults são técnicos, não orçamento.
LLM e efeitos consomem apenas reserva previamente autorizada. Budget ausente não vira teto
US$10 herdado de default. Rate-limit agenda reset, avalia outras contas/hosts/APIs autorizadas
e trabalho local qualificado; espera não paralisa outros jobs.
Três tentativas transitórias 5/20/60 s com jitter até 20%; depois RCA e replan deduplicado.
Duas correções focais sem progresso devolvem ao planejador. Retry exige diagnosticar causa.

Idempotência e fencing obrigatórios em mutation. Claims antigos não escrevem resultado,
efeito novo nem gasto. Custo externo incerto continua reservado até consulta/limite conservador.
Reconciliar efeito desconhecido antes de retry. Pausa/cancelamento bloqueiam novo efeito;
efeito já aceito externamente é observado até término/recuperação, sem fingir que desapareceu.

Rollback de código: revert seletivo, sem apagar logs/checkpoints. Schema é expand/contract;
não dropar tabela enquanto run antigo depende dela. Desativar novo handler não converte owner.
Rollback de release deve comprovar digest e dados restaurados no alvo, conforme binding.

## Validação de cada unidade

Ordem: provar coleta adjacente existente; criar primeiro teste novo quando previsto;
coletar e reproduzir a contraprova; implementar; focal; quick harness; suíte completa;
revisão independente; PR/checks/merge/reachability. Zero coleta é falha.
`VALIDATE_CMD` focal em cada handoff só fica executável depois de criar teste marcado NOVO.
Comandos obrigatórios existentes:

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v --ignore=tests/test_canaletto.py
```

Rodar sequencialmente: ambos executam suítes e podem disputar CPU no teste de latência.
Logs registram exit code, contagem e `[HARNESS_PASS]`; não fabricar marcador.
Testes locais são contraprovas de controle, não aceitação do serviço instalado.
Evidência de release exige TARGET_ENVIRONMENT pelo observador; documental não a substitui.

ManualDependency somente para intenção/acesso/segredo/aceite exclusivo, após alternativas
testadas. Deve conter guia atual tela a tela, URL e todos os campos/seletores, resultado
por passo, canal seguro, probe executado pela identidade correta, blocked_stages,
resume_criteria e notificações deduplicadas. Não solicitar segredo em conversa/JSON.
