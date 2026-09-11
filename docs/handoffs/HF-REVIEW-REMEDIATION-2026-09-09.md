# Handoff de remediação HF — versão 1.0

Data: 09/09/2026. Parent: revisão crítica HF. Origem: pedido do owner para avaliação e aperfeiçoamento de planejamento, especificação e skills. Documento canônico: este arquivo; [diagnóstico e reproduções](../reviews/HF_CRITICAL_REVIEW_2026-09-09.md). Identidade do planejador: coordenador Codex desta tarefa, modelo GPT-6; a conversa é a origem do desenho, não um receipt de aprovação emitido por supervisor externo.

**Estado: `design_specified`.** Este documento resolve o desenho local das correções, mas não despacha desenvolvimento. Antes do despacho, o coordenador aplica `docs/HANDOFF_POLICY.md`, registra a aprovação qualificada contra o hash desta versão e executa o preflight abaixo. Não inventar um registro de autoridade para satisfazer o futuro gate. Não é necessária nova confirmação do owner para decisões técnicas já especificadas; implementação continua com o papel econômico autorizado na tarefa de desenvolvimento.

## Grill, baseline e preflight comuns

Intenção conhecida: melhorar os módulos já implementados e a qualidade dos próximos handoffs. Decisões reutilizadas: núcleo headless; projetos locais de demonstração separados; alta inteligência especifica/revisa; econômico implementa/testa; nenhuma simulação autoriza entrega operacional. Nenhuma dúvida material de produto impede este planejamento. Credenciais de HF-02-05/07 não são necessárias para as correções locais abaixo.

Baseline: HEAD `0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9` mais [hashes dos arquivos revisados](../../.factory/reviews/hf-critical-20260909/source-manifest.json). HF-04/HF-05 e parte de HF-02 estão untracked; arquivos foram produzidos por outras frentes. Antes de tocar em cada unidade:

1. Conferir os paths do ticket contra o manifesto, o handoff vigente e as mudanças dos predecessores. Mudança relevante exige revisão do plano; mudança alheia ao contrato não invalida todo o pacote.
2. Resolver ownership/worktree conforme a skill 04. `.git` é somente leitura nesta tarefa; não declarar branch, commit ou entrega remota que não existem.
3. Registrar plano/versão/hash, executor, orçamento disponível e comandos. Um novo teste só pode ser executado depois de criado; o red inicial deve falhar pelo comportamento esperado, não por importação/arquivo ausente.
4. Executar o controle positivo e a negativa focal. Aceite sem oráculo independente volta ao planejador. Dois ciclos sem progresso sobre a mesma causa também voltam ao planejador com evidência.

Ambiente comum: Python 3.12+, dependências já instaladas, SQLite local quando indicado, UTF-8, relógio injetado e dados sintéticos. Rede, cloud, billing, instalação de dependências e alteração do runtime escolhido não estão autorizados por este pacote. Alterar dependências públicas ou requisitos leva a `needs_replan`.

Recursos/conflitos: cada ticket detém exclusivamente seus arquivos principais; tickets com o mesmo arquivo são sequenciais. Logs em `.factory/test_logs/<ticket>/<run>/`; stores/temp separados por execução. Disparo: preflight positivo mais evidências dos predecessores. Tentativas: duas correções focais, sem loop ilimitado; pytest focal com timeout de 120 s e subprocessos do driver com deadline de 10 s e cleanup explícito. Suítes obrigatórias conservam os timeouts do harness. Não introduzir fallback que transforme prova ausente em sucesso.

Rollback: reverter somente a mudança do ticket contra seu diff/backup, preservando trabalho concorrente; stores de teste descartáveis, sem mover/apagar banco do usuário. Artefatos gerados permitidos além dos paths principais: relatório do ticket e logs. Não alterar os handoffs antigos para apagar defeitos; registrar a versão corretiva e o delta.

## Fronteiras de confiança decididas

| Garantia | Fonte confiável | Comparação obrigatória | Enforcement |
|---|---|---|---|
| Plano aprovado | Registro do supervisor, recebido fora do payload do candidato | ticket, versão/hash do plano, capacidade do planejador, baseline relevante e autorização | gate antes de despacho |
| Revisão independente | Receipt resolvido pelo supervisor | candidato/hash exato, verificador distinto do implementador, papel habilitado, resultado e escopo | gate antes de entrega |
| Evidência atual | Receipt de execução preservado e configuração esperada do supervisor | requisito, alvo, identidade/rota, build/config, validade e relógio | gate na etapa que consome a prova |
| Dependência humana resolvida | Receipt do probe de retomada | dependency/ticket, probe, alvo/config/identidade e versão posterior à mudança | desbloqueio dos descendentes afetados |
| Baseline verificada | Fontes/claims/política vinculadas a manifesto confiável | bytes, refs, grafo e recomputação dos resultados | collect/verify |
| Aprovação do experimento | Sujeito esperado registrado pelo controlador | workflow + digest; decision ID só deduplica | store antes de encaminhar decisão |
| Replay | Operação persistida na mesma transação do efeito | ID + payload canônico + resultado original | runtime antes de mutar estado/outbox/orçamento |

Decisão de arquitetura: a biblioteca de domínio recebe um contexto confiável **injetado**, separado do JSON do candidato. Isso não autentica processos: o supervisor que constrói esse contexto pertence à fronteira de confiança. A integração desse supervisor com identidade/armazenamento reais é posterior e não pode ser simulada como operacional. Fixtures podem simular o contexto para provar a lógica, com rótulo de simulação.

## Ordem e dependências

| Trilha | Sequência | Condição para avançar |
|---|---|---|
| HF-02 transporte | CR-01 → CR-14 → CR-16 | processo real do driver local funciona |
| HF-01 confiança | CR-02 → CR-03 → CR-12 → CR-16 | coleta vinculada e replay sem falsos PASS |
| HF-04 gates | CR-04 → CR-05 → CR-06 → CR-07 → CR-08 | controle positivo confiável e negativas isoladas |
| HF-05 runtime | CR-09 → CR-10 | contrato local não declara entrega; replay não repete efeito |
| Complementos locais | CR-11, CR-13, CR-15 | ownership dos arquivos e testes próprios |

As trilhas podem avançar em paralelo só com ownership real. CR-09 não precisa aguardar infraestrutura nem os reparos do gate: seu recorte local deve rejeitar entrega desde já. A futura ligação HF-04/HF-05 depende de CR-04/05/06 e de handoff próprio do supervisor.

## CR-01 · P1 · Restabelecer o transporte JSON do driver HF-02

Achado: F06. Paths: `spikes/runtime_choice/contracts.py`, `tests/test_runtime_spike_contracts.py`, `tests/test_runtime_spike_native.py`; `driver.py` é leitura, salvo replanejamento que demonstre necessidade. Interface preservada: `LabConfig` e `python -m spikes.runtime_choice.driver --config <json>`.

Decisão: validar/normalizar `root_dir` depois da conversão de tipo compatível com a rota JSON; preservar as restrições de path, tipos de controle e `extra=forbid`. Não remover strict globalmente. Entrada válida: JSON emitido por `LabConfig.model_dump_json()` com raiz absoluta sob temp exclusivo; inválidas: raiz fora do contrato, bool no lugar de controle numérico e campo extra.

Aceite: (1) Python → dump → `model_validate_json` preserva o objeto exato; (2) o processo lê o mesmo arquivo, emite readiness conforme o protocolo vigente, recebe um comando JSONL válido e termina pelo comando de shutdown; (3) efeito persistido confere por leitura do store, sem confiar no bool do driver; (4) inválidos mantêm `CONFIG_INVALID` sem vazar entrada. Criar o teste de subprocesso antes de validar.

`VALIDATE_CMD`: `python -m pytest tests/test_runtime_spike_contracts.py tests/test_runtime_spike_native.py -v`.

## CR-02 · P1 · Vincular claims e limitar promoção no HF-01

Achados: F05 e conflito de claims. Paths: `core/planning/baseline_models.py`, `core/planning/baseline_reconcile.py`, `core/planning/baseline_probes.py`, `tests/test_baseline_reconcile.py`. Entrada: coleta + claims + observações; saída: snapshot com justificativas preservadas.

Decisões: reconciliador valida `source_id`, status lido e igualdade de `source_hash` com observação; locator deve identificar a fonte/fragmento permitido pelo catálogo. Desacordo produz issue estruturada e não sustenta conclusão positiva. Claims remotas importadas permanecem `reported`; mesmo SHA não é atestado. Probe de simulação permanece evidência de simulação; health check prova apenas disponibilidade do endpoint. Preservar observações sanitizadas e IDs no snapshot. Nenhuma promoção nova a `verified` sem atestado e escopo explicitamente reconhecidos; integração do verificador remoto fica fora desta unidade.

Aceite: (1) claim documental válida continua `reported`; (2) hash/source/locator inválidos impedem uso positivo; (3) `remote_git` não atestado e probe simulado não viram integração/operação verificadas; (4) positivo+negativo do mesmo escopo resulta em `contradicted` e issue, preservando ambos; escopos distintos não conflitam artificialmente; (5) JSON preserva a observação usada. Compatibilidade do schema: incrementar versão se campos obrigatórios alterarem a leitura e rejeitar versão não suportada; não inferir metadados ausentes em snapshots antigos.

`VALIDATE_CMD`: `python -m pytest tests/test_baseline_reconcile.py tests/test_baseline_probes.py -v`.

## CR-03 · P1 · Verificar a interpretação do snapshot HF-01

Depende de CR-02. Achado: F04. Paths: novo `core/planning/baseline_verify.py`, `core/planning/baseline_cli.py`, `core/planning/baseline_models.py`, `tests/test_baseline_cli.py`.

Interface nova: `verify_snapshot(snapshot, *, root, manifest) -> VerificationReport`; CLI `verify --snapshot <file> --root <root> --manifest <file>`. Manifesto selecionado pelo operador/supervisor contém versão de política, paths relativos contidos no root e hashes de catálogo, claims e observações persistidas. O comando collect deve emitir o manifesto para ser capturado pelo supervisor; confiar nele exige seleção independente do snapshot. Não autorizar o candidato a substituir simultaneamente ambos como se o hash autenticasse a origem.

Decisão: reconstruir coleta/reconciliação com os inputs vinculados e comparar os campos derivados, sem repetir rede durante replay. Conferir refs, IDs, ciclos, assessments, issues, blockers e readiness. `verify` sem dados de replay pode emitir diagnóstico de estrutura, mas não `[BASELINE_VERIFY_PASS]`. Nova versão deve falhar explicitamente para dados antigos sem contexto suficiente.

Aceite: baseline intacta recomputada passa; mutações isoladas em readiness, dimensão, blocker, claim ref, dependência e manifesto falham, mesmo com fingerprint de fontes inalterado. Fonte alterada também falha. Testar a interface e o CLI, não comparar apenas um hash escrito pelo teste. Erro de corrupção usa exit 3; falta de fonte requerida usa exit 2 (consolidado em CR-12).

`VALIDATE_CMD`: `python -m pytest tests/test_baseline_cli.py tests/test_baseline_reconcile.py -v`.

## CR-04 · P1 · Introduzir o contexto de verificação HF-04

Achados: F01/F02. Paths: novo `core/workflow/verification.py`, `core/workflow/contracts.py`, novo `tests/test_workflow_verification.py`. `readiness.py` e runtime são leitura nesta unidade. Não declarar o gate corrigido antes de CR-05/06.

Interface decidida: `VerificationContext`, valor imutável com `now`, `policy_version`, `plan_digest`, `candidate_digest`, `config_version`, alvo/identidade/rota esperados e mapas de aprovações/receipts por referência. `GatePolicy` fornece requisitos por etapa, papéis habilitados, validade por requisito e dispensas tipadas por ticket/versão. Recibos contêm produtor, sujeito, requisito, hash do artefato, resultado, modo, instante e vínculos de versão. O contexto não é campo de `WorkflowHandoff` nem é desserializado de JSON recebido do implementador.

Defaults: contexto ausente, aprovação desconhecida, requisito/política desconhecido e dispensa sem autoridade bloqueiam. Lista de requisitos do candidato é informativa e não reduz a política. Uma política local de contratos pode ter zero provas operacionais na etapa de planejamento; entrega nunca usa dispensa genérica de evidência operacional. Não introduzir blockchain, assinatura fictícia, novo serviço ou acesso remoto neste contrato.

Aceite: mapa defensivamente imutável; referências duplicadas/conflitantes e datas sem timezone rejeitadas; identidade/build/config esperados independem do payload candidato; fixtures distinguem produtor confiável de declaração importada. Definir exportação de diagnóstico que não inclua credenciais. JSON dos contratos de dados permanece verificável; contexto interno não precisa de transporte público.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_verification.py tests/test_workflow_contracts.py -v` (o primeiro arquivo será criado).

## CR-05 · P1 · Aplicar política, autoridade e etapa no gate

Depende de CR-04. Achados: F01/F03. Paths: `core/workflow/readiness.py`, `core/workflow/verification.py`, `tests/test_workflow_contracts.py`, `tests/test_workflow_verification.py`.

Interface: `ReadinessGate.evaluate(handoff, *, context=None, target_state=None)`. Contexto ausente retorna inelegível com código estável; `require_delivery` e `mark_delivered` recebem o mesmo contexto. `target_state=None` consulta a etapa atual, sem prometer avanço. Transição explícita diferente do estado atual passa por `validate_transition`; consulta/replay não inventa self-edge. Não usar fallback genérico que faça cancelado, falho ou planejamento virar prontidão de release.

Exigir aprovação do plano resolvida pelo supervisor, capacidade habilitada e hash exato. Para entrega, exigir receipt de revisão para o candidato, revisor distinto do implementador, política completa e prova do alvo. Requisitos vêm de `GatePolicy`, com mapeamento explícito entre workflow/readiness; `required_for` não é ordinal arbitrário. Etapas futuras não bloqueiam estágio anterior; todas as provas de entrega são cobradas antes do terminal.

Aceite: (1) controle de planejamento válido começa sem prova operacional futura; (2) lista vazia do candidato não elimina requisito; (3) aprovação falsa/economy autodeclarado/receipt de outro candidato bloqueiam; (4) cancelado não avança; (5) fluxo válido por etapas funciona; (6) entrega sem revisão/target bloqueia. Ajustar testes positivos antigos para contexto confiável de simulação; nunca fazê-los passar reinstalando a confiança nas flags.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_contracts.py tests/test_workflow_verification.py -v`.

## CR-06 · P1 · Calcular validade e conferir o sujeito da evidência

Depende de CR-05. Achado: F02. Paths: `core/workflow/readiness.py`, `core/workflow/verification.py`, `tests/test_workflow_verification.py`.

Decisão: para cada requisito da etapa, resolver o receipt no contexto e comparar ticket/plano/candidato/configuração, ambiente/identidade/rota, capacidade e hash. Calcular `0 <= now - observed_at <= max_age` com UTC; `max_age` vem da política. Configuração/credencial/versão trocada invalida receipt anterior mesmo dentro do TTL. `freshness=current` recebido não altera o resultado. Valores ausentes não recebem default que os faça atuais.

Aceite: controle positivo inteiramente vinculado passa; testar cada dimensão inválida isoladamente, data antiga, futura, limite exato de validade e diferença de um instante além do limite. Teste não pode se tornar verde apenas porque uma aprovação inválida compartilhada passou a bloquear todos os casos. Saída explica o motivo e inclui IDs, sem valores sensíveis.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_verification.py tests/test_workflow_contracts.py -v`.

## CR-07 · P2 · Fazer Grill e dependência humana refletirem a resolução

Depende de CR-06. Achado: F09. Paths: `core/workflow/contracts.py`, `core/workflow/verification.py`, `core/workflow/readiness.py`, `tests/test_workflow_verification.py`.

Decisões: toda decisão material precisa de resposta/seleção válida com origem, ou referência verificável a uma decisão anterior. `pending_questions=[]` não apaga decisões sem resposta. Pressupostos reversíveis explícitos são categoria separada e não pedem confirmação artificial.

Resolução humana exige `resolution_receipt_ref` que resolve para o `final_probe` correto, dependency/ticket/alvo/identidade/configuração e versão posterior à mudança. `resolved_at` sozinho não desbloqueia. Declarar por dependência quais estágios e tickets ela bloqueia; resposta do owner pode disparar probe, não substituí-lo. Efeitos de notificação/automação ficam fora deste ticket.

Aceite: decisão material sem resposta bloqueia; decisão reutilizada com origem passa; preferência opcional assumida não cria waiting_human; receipt errado/antigo/ausente mantém bloqueio; receipt válido desbloqueia apenas descendentes dependentes e replay não muda a resolução.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_contracts.py tests/test_workflow_verification.py -v`.

## CR-08 · P2 · Reduzir exposição de segredos em manifestos

Depende de CR-07 por ownership. Achado: F10. Paths: `core/workflow/contracts.py`, novo `core/workflow/safe_export.py`, novo `tests/test_workflow_safe_export.py`.

Decisão: URLs de manifesto aceitam endereço sem userinfo e sem query/fragmento que transporte credencial. Serviços são identificadores/referências, sem `password=...` ou DSN. Exportação pública passa por `safe_export` e diagnósticos de validação retornam código/localização, sem `input` bruto. Manter referências de secret manager; não inspecionar valores reais de segredos. Não prometer detecção universal de segredos em texto livre.

Aceite: sentinelas sintéticas em userinfo/query/services e erro não aparecem em JSON/log; URLs/ref seguras mantêm roundtrip; valores suspeitos são rejeitados/sanitizados segundo o campo, sem silenciosamente converter uma URL inválida em alvo diferente. Documentar a API de exportação no docstring e atualizar consumidores somente se existirem nos paths aprovados; novos consumidores exigem binding próprio.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_safe_export.py tests/test_workflow_contracts.py -v`.

## CR-09 · P1 · Conter os estados do runtime local HF-05

Achado: F07. Paths: `core/workflow/runtime.py`, `tests/test_workflow_runtime.py`. Depende da presença da versão local HF-05 e preflight, sem depender de CR-04.

Decisão: este runtime ainda não possui verificador confiável conectado. `register_run` e `transition_run` rejeitam `DELIVERED` com erro de domínio sanitizado antes de qualquer escrita; não aceitar `initial_state` como rota de importação privilegiada. Persistência/reload de snapshot externo não pode contornar essa regra. Futuro ingresso operacional terá API distinta, plano próprio e receipt verificado; não acrescentar flag `allow_delivery` neste reparo.

Aceite: ambas as reproduções de F07 rejeitam sem run/outbox indevidos; fluxo local de implementação/validação/revisão continua persistível; terminais locais legítimos continuam bloqueando despacho; reload não fabrica status operacional. Ler SQLite/outbox independentemente após o erro.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_runtime.py -v`.

## CR-10 · P1/P2 · Deduplicar a operação inteira no HF-05

Depende de CR-09. Achados: F08/F16. Paths: `core/workflow/runtime.py`, `tests/test_workflow_runtime.py`.

Decisão: adicionar tabela/registro de operações na mesma transação do estado/outbox, com chave, sujeito, payload canônico, versão e resultado. `transition_run` consulta esse registro antes de mutar; replay idêntico devolve resultado original sem reaplicar estado nem emitir novo evento. Nova intenção usa nova chave. Reuso conflitante falha mesmo quando o target já é o estado atual.

`complete_job` vincula lease, worker, outcome, custo efetivamente cobrado e erro normalizado. Replay idêntico retorna o resultado daquela conclusão; custo/erro/outcome alterado é conflito e não muda cobrança. Preservar resultado antigo mesmo se o job teve nova tentativa. Schema novo usa migração transacional local versionada; registros legados sem receipt não podem ser tratados como novos para reaplicar efeitos. Nenhuma migração em store real nesta tarefa; testar cópia descartável.

Aceite: replay imediato, após ciclo de rework, após nova tentativa e após reload não altera estado/outbox/orçamento; variações conflitantes falham; falha injetada entre receipt e efeito faz rollback completo. Controle: duas operações novas com chaves distintas geram duas transições/eventos legais.

`VALIDATE_CMD`: `python -m pytest tests/test_workflow_runtime.py -v`.

## CR-11 · P2 · Corrigir dependências entre prefixos

Achado: F14. Paths: `core/planning/baseline_sources.py`, `tests/test_baseline_sources.py`.

Decisão: tokens com prefixo completo consomem seus spans antes de interpretar números/ranges sem prefixo. Exemplo válido: tabela HF com `DF-11` produz somente `DF-11`; `HF-01, 02` produz os dois HF correspondentes conforme a gramática vigente. Ambiguidade/texto não estrutural não inventa vínculo.

Aceite: prefixos mistos, ranges, repetidos, desconhecidos e prosa; sem dependência fantasma/ciclo artificial. `VALIDATE_CMD`: `python -m pytest tests/test_baseline_sources.py -v`.

## CR-12 · P2 · Alinhar códigos de saída do HF-01

Depende de CR-03. Achado: F14. Paths: `core/planning/baseline_cli.py`, `tests/test_baseline_cli.py`.

Decisão: manter relatório parcial, mas retornar 2 se fonte obrigatória faltar, 3 para corrupção/grafo/replay inválido e 0 apenas para a categoria de sucesso contratada. Categorias de erro tipadas, mensagem sanitizada. Falta opcional é visível sem ser promovida a fonte lida.

Aceite: subprocesso collect/verify para cada categoria confere exit, stderr/stdout e snapshot diagnóstico; parser de argumentos continua comportamento normal de argparse. `VALIDATE_CMD`: `python -m pytest tests/test_baseline_cli.py -v`.

## CR-13 · P2 · Cumprir limites dos probes HF-01

Achado: F14. Paths: `core/planning/baseline_probes.py`, `tests/test_baseline_probes.py`.

Decisão: prazo máximo de 3 s por probe e corpo máximo 65.536 bytes, incluindo leitura; configurar prazo maior é erro. Testes usam transporte/relógio controlados e não conectam serviços reais. Excesso vira resultado estruturado, sem armazenar o corpo sensível.

Aceite: 65.536/65.537 bytes, prazo exato/excedido, configuração acima de 3 s e falha de transporte; metadados de resultado preservam os limites usados. `VALIDATE_CMD`: `python -m pytest tests/test_baseline_probes.py -v`.

## CR-14 · P2 · Preservar erros no protocolo do driver HF-02

Depende de CR-01. Achado: F12. Paths: `spikes/runtime_choice/native_adapter.py`, `spikes/runtime_choice/driver.py`, `tests/test_runtime_spike_native.py`.

Decisão: run desconhecido gera evento/código `RUN_NOT_FOUND`; SQLite sem acesso na inicialização gera `STORE_UNAVAILABLE` e saída não zero sanitizada. Erro de config permanece `CONFIG_INVALID`. Não criar outro store, mudar runtime nem emitir readiness depois de falhar startup. Converter erros esperados nas bordas; não esconder defeito interno com `except Exception: pass`.

Aceite: subprocesso real com config válida cobre observe desconhecido e store inacessível; nenhum traceback/segredo; processo é encerrado e temporários liberados; leitura independente confirma ausência de fallback. `VALIDATE_CMD`: `python -m pytest tests/test_runtime_spike_native.py -v`.

## CR-15 · P2 · Vincular aprovação ao sujeito esperado do experimento

Achado: F11. Paths: `spikes/runtime_choice/effect_store.py`, `spikes/runtime_choice/effect_server.py`, `tests/test_runtime_spike_effects.py`.

Interface de domínio nova: `bind_approval_subject(workflow_id, payload_digest)`, antes de aceitar decisão. Binding idempotente só aceita o mesmo digest; troca é conflito. `record_approval` compara com esse binding mesmo para decision ID novo. Ausência de binding rejeita. O binding deve ser criado pelo controlador confiável; não oferecer endpoint ao candidato que permita redefini-lo. O servidor existente só encaminha decisão para sujeito já registrado.

Aceite: primeiro digest errado, novo ID/digest errado e tentativa de rebinding falham sem efeito; decisão válida e replay idêntico funcionam; scope de outro workflow não contamina o primeiro. Integração com espera/DBOS continua pendente e não é validada por este teste de store.

`VALIDATE_CMD`: `python -m pytest tests/test_runtime_spike_effects.py -v`.

## CR-16 · P2 · Qualificar resultados e oráculos do experimento

Depende de CR-01/02/03/14. Achados: F13/F15. Paths: `spikes/runtime_choice/contracts.py`, `tests/test_runtime_spike_contracts.py`, `tests/test_runtime_spike_effects.py`, `tests/test_baseline_catalog.py`.

Decisão: `ScenarioResult` inclui `environment_ref`, `validation_mode` (`real_lab`, `target_environment`, `mock_only`) e `target_differences`; modo desconhecido é erro, dado antigo sem origem não recebe default de alvo. A comparação conserva origem por rodada; não promove mock nem mixed env a evidência de operação. Vincular catálogo congelado a digest esperado de arquivo revisado; só comprimento 64 é insuficiente. Executar fixtures A1–A10 no pipeline de negócio com expected separado e verificar diferenças observáveis.

Aceite: roundtrip de cada modo, rejeição do ausente/inválido, agregação com diferenças preservadas; mutar resultado esperado no catálogo sem reaprovação deve falhar; fixture de contradição/fonte ausente percorre o reconciliador e não apenas compara constantes. Novas versões do catálogo requerem revisão e hash explícito; o adaptador não altera o expected para passar.

`VALIDATE_CMD`: `python -m pytest tests/test_runtime_spike_contracts.py tests/test_runtime_spike_effects.py tests/test_baseline_catalog.py -v`.

## Fechamento e próximo destinatário

Em cada ticket, além do comando focal, executar os gates obrigatórios:

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v
```

Registrar contagens, exit codes, modo, logs, hashes antes/depois e exemplos aceitos/rejeitados. Reexecutar a reprodução independente pertinente; se uma mudança de API exigir adaptar o teste de auditoria, preservar a intenção e registrar o delta. Não esconder uma falha por alterar o fixture compartilhado ou por depender da primeira rejeição genérica.

O destinatário imediato é o coordenador de desenvolvimento, que confirma preflight/aprovação e despacha a primeira unidade disponível a executor econômico. O revisor independente fecha achados por evidência. A entrega remota segue a regra Git do projeto; sem escrita em `.git`, relatório local não vira `delivered`.

Depois de CR-01/02/03/04/05/06/09/10, reavaliar a base antes de expandir HF-05 ou usar seus estados como autorização. HF-02-05/07 mantêm `waiting_access` até o probe real pertinente; o controlador/oracle e a decisão de runtime não podem ser substituídos por estes ensaios locais.
