# Revisão crítica dos incrementos HF — 09/09/2026

**Parecer: `changes_required` nas superfícies abaixo.** Os módulos formam uma base útil, mas algumas garantias de confiança, prontidão e idempotência ainda não correspondem ao comportamento das APIs públicas. Os testes existentes verdes não autorizam tratar essas garantias como concluídas.

Esta avaliação atende ao pedido do owner de revisar o que foi desenvolvido, melhorar orientações e atualizar as skills. Não altera código de produto nem substitui o roadmap. As correções estão no [handoff de remediação](../handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md).

## Base, escopo e evidência

- HEAD observado: `0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9`, com alterações locais e arquivos untracked. Esse SHA sozinho não identifica o código revisado: consultar o [manifesto por arquivo](../../.factory/reviews/hf-critical-20260909/source-manifest.json).
- Inspeção principal: `core/planning/` (HF-01), contratos/driver/store nativo de `spikes/runtime_choice/` (HF-02), `core/workflow/contracts.py` e `readiness.py` (HF-04).
- HF-05 apareceu durante o trabalho em outra frente. Foi acrescentada uma revisão focal de `core/workflow/runtime.py`, dos testes e do handoff local disponíveis naquele momento. Não é certificação de todo o scheduler nem revisão de uma entrega congelada de HF-05.
- Não foram executados DBOS/PostgreSQL, integração cloud, worker remoto, produção ou ações Git de escrita. HF-02-05/07 continuam dependentes do acesso já registrado; cenários declaradamente pendentes/unsupported não foram contados como defeitos de implementação.
- Contraexemplos usam dados sintéticos, caminhos locais descartáveis e APIs públicas. Os testes de auditoria ficam fora de `tests/`, com execução explícita e resultado vermelho registrado. Não houve alteração dos oráculos existentes para obter verde.

## O que está bem encaminhado

Há separação entre domínio e I/O, contratos Pydantic com campos extras proibidos, enums, IDs e relações básicas validados. O HF-01 torna fontes e diferenças de status inspecionáveis; o HF-02 separa efeito observável do driver e declara capacidades ainda não ensaiadas. O HF-04 já rejeita vários casos usuais de mock, evidência explicitamente stale e dependência pendente. O HF-05 usa transação SQLite e relógio injetável, o que permite investigar replay e recursos sem serviços externos.

Essas propriedades reduzem o custo de reparo. A fragilidade recorrente está na passagem de **declaração recebida** para **fato verificado** e na distância entre o teste de uma função auxiliar e o caminho usado pelo consumidor.

## Achados impeditivos

### F01 · P1 · HF-04 pode autorizar entrega sem política ou provas

Em `core/workflow/contracts.py`, `WorkflowHandoff.required_evidence` admite lista vazia. `ReadinessGate.evaluate`, em `core/workflow/readiness.py`, percorre essa lista enviada pelo próprio candidato e considera elegível quando não acumulou razões. Um payload válido com ambas as listas de evidência vazias, estado `independent_review` e manifesto `target_environment` obtém autorização de entrega.

O mesmo caminho aceita `planner_tier=economy`, aprovação não registrada e referência de artefato não resolvida. O nome do estado substitui o recibo de revisão independente. Não há registro confiável que comprove qualificação, aprovação, revisão ou a identidade real do alvo.

Reprodução: `test_delivery_rejects_empty_evidence_policy` e `test_delivery_requires_trusted_planner_and_review_receipt`. Corrigir primeiro a origem dos requisitos e da autoridade: política e receipts fornecidos pelo supervisor, vinculados ao plano/candidato. Acrescentar apenas `min_length=1` ou um novo `approved=True` não fecha o mecanismo. A especificação local HF-04 é insuficiente nessa fronteira; a exigência de autoridade já está explícita em `docs/HANDOFF_POLICY.md`.

### F02 · P1 · HF-04 confia no rótulo de atualidade e ignora vínculos

O gate compara `environment_ref`, `result`, `freshness` e presença de texto em `evidence_ref`. Não calcula idade nem compara identidade/build/configuração com valores esperados de uma fonte confiável. Data de 2000 marcada `current`, outra identidade e outro build foram aceitos separadamente.

Reprodução: três variantes de `test_delivery_rejects_stale_or_unbound_evidence`. A correção precisa de relógio injetado, validade por requisito e identidade/configuração/candidato esperados. Um timestamp preenchido não é prova atual. O modelo não contém hoje todos os valores esperados necessários; há trabalho de contrato antes do enforcement.

### F03 · P1 · HF-04 não aplica a tabela de transições no gate e exige provas na etapa errada

`validate_transition` existe, mas `ReadinessGate.evaluate` não o chama ao receber uma transição proposta. Um handoff `cancelled` foi considerado elegível para `implementing_economy`; o fallback ainda pode produzir `ready_for_release` para estados sem essa semântica.

No sentido inverso, `EvidenceRequirement.required_for` é ignorado: uma prova marcada para `operationally_verified` é cobrada já em `ready_for_handoff`. Isso cria dependência circular se a implementação precisa começar para produzir a prova operacional.

Reprodução: `test_cancelled_job_cannot_be_eligible_to_implement` e `test_future_operational_evidence_does_not_block_current_plan`. Distinguir consulta de estado, transição proposta e commit; todos os caminhos que avançam devem aplicar a mesma política de etapa. Testar também o fluxo legítimo completo.

### F04 · P1 · HF-01 verify emite PASS para snapshot adulterado

`core/planning/baseline_cli.py:_verify` confere schema, unicidade e fingerprints de fontes. Não reconstrói assessments/readiness nem verifica todas as relações entre claims, itens, issues e grafo. Após collect legítimo, a reprodução removeu bloqueios, marcou integração/operação `verified`, definiu HF-02 `ready`, acrescentou uma claim inexistente e um autociclo. Sem mudar as fontes, `verify --root` retornou `[BASELINE_VERIFY_PASS]`, exit 0.

O problema também ocorreria por corrupção acidental da projeção. Hash de fontes não autentica a interpretação armazenada. Verificação precisa reconstruir resultados a partir de entradas vinculadas a um manifesto confiável ou comparar um atestado independente, rejeitando cada alteração crítica isoladamente.

### F05 · P1 · HF-01 promove relato/simulação e usa claim de fonte desatualizada

`core/planning/baseline_reconcile.py` promove claim importada `remote_git` quando `candidate_sha` coincide com a base, sem recibo de PR/merge/proveniência. Um probe `ok`, `validation_mode=simulation`, ambiente `unit-test` promove operação a `verified`; a observação original nem permanece no snapshot. Um HTTP OK não comprova toda a operação de um item.

Além disso, collect aceitou uma claim com `source_hash` diferente dos bytes coletados; o verify subsequente também passou. O teste do catálogo fixo não protege entradas futuras do CLI. Vincular fonte, locator, hash, capacidade, modo e receipt ao assessment; preservar evidência sanitizada. Conflitos positivo/negativo no mesmo escopo também precisam sobreviver à reconciliação.

### F06 · P1 · HF-02 rejeita o JSON produzido pela própria configuração

`spikes/runtime_choice/contracts.py:LabConfig` transforma `root_dir` em `Path` em validador `before`. A leitura estrita por `model_validate_json`, usada em `driver.py:load_config`, rejeita esse valor já convertido com `string_type`. Um `LabConfig` válido construído em Python e gravado com `model_dump_json()` fez o processo real terminar em exit 2, `CONFIG_INVALID`, sem iniciar o protocolo.

Os testes existentes usam objeto Python e `StringIO`, ou só serializam. Ajustar a fase da validação desse campo e testar produtor → arquivo → subprocesso → protocolo. Não remover strict globalmente para consertar um campo. As diferenças entre validação Python/JSON estão na documentação oficial de [strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/); as APIs de criação/cópia estão em [models](https://docs.pydantic.dev/latest/concepts/models/).

### F07 · P1 · HF-05 oferece caminho de entrega que contorna o gate

O handoff HF-05 local diz que esse runtime não produz `delivered`. Entretanto `WorkflowRuntime.register_run(initial_state=DELIVERED)` persiste esse estado, e uma sequência de transições legais até `independent_review → delivered` também funciona sem handoff, evidência ou `ReadinessGate`.

Reprodução: duas variantes de `test_local_runtime_cannot_declare_delivery_without_evidence`. O fato confirmado é a declaração indevida no store local; não houve deploy ou efeito remoto. Como o slice é explicitamente local, bloquear esse terminal em todos os seus caminhos públicos até existir a integração com verificador confiável. Essa correção continua necessária mesmo depois de reparar o gate HF-04.

### F08 · P1 · HF-05 deduplica o evento, mas reaplica a transição

`transition_run` usa `_insert_event` para dedupe, porém não distingue evento novo de replay antes de atualizar o estado. Após `implementing → validating → failed_validation → implementing`, reenviar a primeira chave `implementing → validating` muda novamente o estado, sem criar novo evento. O estado e a história causal deixam de representar a mesma execução.

Reprodução: `test_old_transition_replay_does_not_reapply_state_mutation`. Idempotência deve registrar identidade/payload/resultado da operação e decidir replay antes da mutação, na mesma transação. Testar reenvio depois de outras transições e depois de reload, além do reenvio imediato.

## Outros achados concretos e lacunas de aceite

| ID / prioridade | Superfície | Observado e orientação |
|---|---|---|
| F09 · P2 | HF-04 `GrillRecord`, `ManualDependency` | Decisão material sem resposta passa com `pending_questions=[]`; `resolved_at` basta para retirar dependência humana, sem receipt do `final_probe`. Exigir resolução com origem e probe vinculado; uma preferência opcional explícita pode continuar assumida. |
| F10 · P2 | HF-04 `EnvironmentManifest` | Sentinela sintética em `?token=` de endpoint e em `services=["password=..."]` é serializada. O contrato promete mais proteção do que oferece. Reduzir texto livre, sanitizar exportação/erros e documentar limite de detecção. Nenhum segredo real foi usado. |
| F11 · P2 | HF-02 `NativeEffectStore.record_approval` | Novo `decision_id` aceita outro digest para o mesmo workflow. Falta o digest esperado registrado antes da primeira decisão. Confirmada a falha do filtro; o efeito no futuro adaptador DBOS não foi ensaiado. |
| F12 · P2 | HF-02 `NativeAdapter.observe` / driver | Run inexistente causa `AttributeError`; SQLite inacessível causa `OperationalError` na construção direta. Erros esperados precisam virar resultado sanitizado do protocolo. O P1 do JSON mascara hoje parte do caminho CLI. |
| F13 · P2 | HF-02 `ScenarioResult` | Rejeita como extras `environment_ref`, `validation_mode` e `target_differences`, exigidos pelo complemento do handoff. Definir origem de cada resultado antes da comparação futura. |
| F14 · P2 | HF-01 coleta/parser/probes | Fonte required ausente retorna exit 0; `DF-11` em tabela HF vira também `HF-11`; limite implementado de probe (10 s padrão, 2 MiB) diverge de 3 s/64 KiB especificados. Corrigir cada fronteira com casos exatos. |
| F15 · P2 | Oráculos HF-01/HF-02 | Teste de hash verifica apenas comprimento 64; teste de fixtures verifica rótulos esperados da própria fixture. Executar o pipeline e provar que uma mutação indevida derruba a comparação. |
| F16 · P2 | HF-05 `complete_job` | Replay com mesmo outcome e custo alterado de 0,25 para 9 é aceito silenciosamente. Não houve gasto duplicado observado; houve aceitação de payload conflitante. Vincular todo o payload significativo ao receipt, preservando a cobrança original. |

Detalhes, linhas e observações independentes de HF-01/HF-02 estão na [revisão auxiliar preservada](../../.factory/reviews/hf-critical-20260909/baseline-runtime-review.md). Concorrência real entre processos, justiça sob carga prolongada e recuperação após crash de HF-05 não foram cobertas nesta revisão focal; permanecem limites, sem declaração de aprovação.

## Mudanças efetivamente aplicadas nas skills

Foram atualizadas as skills **00, 02, 04, 05 e 06**, em `.agents/skills/`, sincronizadas em `.claude/skills/` e nas cópias pessoais correspondentes em `C:/Users/guigc/.codex/skills/`. A skill 02 ganhou `references/contract-review-patterns.md`. A personal 04 recebeu também as referências vigentes de worktree/entrega. O [manifesto de instalação](../../.factory/reviews/hf-critical-20260909/skill-updates.json) registra hashes antes/depois e backup.

As orientações agora exigem origem confiável e comparação concreta para cada garantia; prova produzida/consumida na etapa correta; testes pelo transporte real; controles positivos e negativos independentes; baseline por hash; e distinção entre implementado localmente, integrado e operacional. A melhoria contínua passou a consolidar regras contraditórias e registrar limites, sem prometer ausência de erros. O Grill reaproveita decisões existentes e só pergunta quando falta intenção material do owner.

Não foi criada regra genérica baseada em um único caso de gosto. Os mecanismos de confiança apareceram em HF-01, HF-04 e HF-05; o problema de transporte foi reproduzido no HF-02. Alterar a skill não corrigiu esses módulos: os achados continuam abertos.

## Qualidade da revisão e limites da avaliação das skills

Houve uma revisão delegada de HF-01/HF-02, da mesma família do coordenador, com reproduções independentes. Uma segunda rota local `gpt-review` não concluiu no timeout. `qwen-fast` concluiu a leitura de HF-04, mas suas quatro alegações não se sustentaram frente ao código/testes e foram descartadas. Não houve consenso independente de outra família nem chamada paga.

Um ensaio separado com Qwen recebeu a skill candidata e uma demanda realista, sem resposta esperada. Produziu um handoff insuficiente: omitiu autoridade/temporalidade, inventou interfaces e respostas do owner e propôs API Pydantic antiga. O ensaio foi **reprovado**. Acrescentou-se uma instrução explícita de revisão semântica; a versão final não foi requalificada por novo ensaio independente. Sintaxe e espelhos válidos não demonstram eficácia geral. O [registro de avaliação](../../.factory/reviews/hf-critical-20260909/skill-evaluation.json) conserva essa limitação.

## Validação e reprodução

As reproduções adicionais HF-04/HF-05 retornaram **15 failed, 1 passed**, com falhas de asserção dos contraexemplos, não de descoberta. São casos parametrizados, não quinze mecanismos independentes. O controle positivo verifica serialização; não certifica autoridade. Ao implementar reparos, criar primeiro um controle plenamente confiável para cada negativa, para que uma rejeição genérica de autoridade não mascare outro defeito.

```powershell
python -m pytest .factory/reviews/hf-critical-20260909/test_gate_regressions.py .factory/reviews/hf-critical-20260909/test_runtime_regressions.py -q -p no:cacheprovider
python -B .factory/reviews/hf-critical-20260909/reproduce_baseline_runtime.py
```

Os 13 testes existentes de HF-04 e os 30 testes focais selecionados pelo revisor auxiliar passaram. Os resultados finais dos dois comandos obrigatórios, contagens, hashes e logs estão no [registro de validação](../../.factory/reviews/hf-critical-20260909/validation.json). A suíte normal e os contraexemplos de revisão devem ser lidos juntos.

Não há commit/push/PR/merge deste trabalho. A conclusão é uma revisão e melhoria local de instruções, com remediação especificada; não é entrega remota do desenvolvimento nem prontidão operacional dos módulos.
