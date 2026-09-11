# Revisão crítica HF-01/HF-02 — 2026-09-09

Veredito técnico: **REJECT para aceite das superfícies afetadas**. Há um bloqueio reproduzível no driver HF-02 por arquivo JSON e falsos positivos reproduzíveis no verificador/reconciliador HF-01. Não se trata de revisão de outra família de modelos.

Escopo: leitura de MISSION.md, FACTORY_RULES.md, AGENTS.md, core/planning/, spikes/runtime_choice/, testes associados, handoffs HF-01/HF-02 e relatórios HF-01/HF-02. Nenhum código, skill ou Git foi modificado por este revisor. Escritas exclusivamente nesta pasta de logs. Sem acesso à rede, serviços externos, DBOS/PostgreSQL ou contas pagas. Stores SQLite descartáveis foram usados diretamente; nenhum servidor HTTP foi iniciado.

## Reproduções

Comando executado a partir de C:\dev\DarkFac:

```powershell
python -B .factory/test_logs/hf-critical-review/agent-runtime/reproduce.py
```

Evidências finais: `run-3a07714b/results.json`, `run-3a07714b/baseline_stdout.txt`, `run-3a07714b/source_hashes.json`; fontes e snapshots sintéticos ficam no mesmo diretório. Cada execução cria outra pasta exclusiva.

Validação dos testes existentes, sem cache e com temporários confinados:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest tests/test_baseline_catalog.py tests/test_baseline_reconcile.py tests/test_baseline_cli.py tests/test_baseline_probes.py tests/test_runtime_spike_contracts.py -q -p no:cacheprovider --basetemp .factory/test_logs/hf-critical-review/agent-runtime/pytest-temp
```

Resultado: **30 passed in 0.24s**. Os dois gates completos do repositório não foram repetidos por este subagente; a tarefa foi uma revisão focal sem alterações de comportamento. Nenhuma conclusão sobre DBOS ou produção decorre desses testes.

## Achados prioritários

### P1 — HF-02 rejeita configuração válida no caminho CLI real

Local: `spikes/runtime_choice/contracts.py:242–252`; chamada em `spikes/runtime_choice/driver.py:38`.

`LabConfig` é estrito. O validador `before` de `root_dir` devolve `Path`; em `model_validate_json`, o schema de Path espera uma string JSON e rejeita o valor já convertido com `string_type`. Uma configuração construída com sucesso em Python e salva por `model_dump_json()` falha ao ser lida pelo próprio driver.

Comando direto que reproduziu exit 2 e stderr `CONFIG_INVALID`, sem stdout:

```powershell
python -B -m spikes.runtime_choice.driver --config .factory/test_logs/hf-critical-review/agent-runtime/run-3a07714b/adapter/valid-config.json
```

Impacto: HF-02-04 não funciona pelo protocolo de configuração por arquivo; bloqueia o futuro controlador de processos. `tests/test_runtime_spike_native.py:131` chama `run_jsonl` com um objeto Python e `StringIO`; `tests/test_runtime_spike_contracts.py:41` só serializa e não desserializa. Ambos evitam a fronteira quebrada.

RCA em cinco passos: driver encerra antes do startup → load_config rejeita o JSON → conversão antecipada entrega Path ao schema JSON estrito → testes exercitam a construção Python → aceite não obrigou ida e volta do formato e processo real. Corrigir a fase de validação do path e adicionar teste do CLI real que aguarde readiness, envie JSONL e observe o efeito independente.

### P1 — HF-01 verify aceita prontidão e evidências forjadas

Local: `core/planning/baseline_cli.py:126–145`; fingerprint limitado às observações em `core/planning/baseline_reconcile.py:32–35`.

Após um collect legítimo, a reprodução alterou o snapshot para `hf02_readiness=ready`, retirou blockers/issues, marcou integração/operação como verified, referenciou uma claim inexistente e inseriu dependência de DF-11 sobre si mesma. Não alterou os arquivos-fonte nem seu fingerprint. `verify --root` emitiu `[BASELINE_VERIFY_PASS]`, exit 0.

```powershell
python -B -m core.planning.baseline_cli verify --snapshot .factory/test_logs/hf-critical-review/agent-runtime/run-3a07714b/baseline/out/snapshot.json --root .factory/test_logs/hf-critical-review/agent-runtime/run-3a07714b/baseline
```

O verificador confere schema superficial, unicidade de fontes/itens e hashes dos arquivos; não confere refs de claims/issues, integridade dos assessments, grafo nem prontidão derivada. Contraria o contrato explícito em HF-01.md:72. Não precisa haver adversário: corrupção manual ou um bug de projeção já produz recibo verde indevido.

RCA: snapshot inválido recebe PASS → verificador aceita campos derivados → fingerprint cobre só fonte → não há reconstrução/validação das relações → testes negativos adulteram apenas o fingerprint. Definir manifesto que vincule catálogo, política, claims e observações; revalidar hashes/refs/grafo e derivar readiness; adicionar mutações independentes para cada campo crítico. Hash de conteúdo não substitui proveniência confiável.

### P1 — HF-01 promove evidência importada/simulada a verified

Local: `core/planning/baseline_reconcile.py:145–154`.

Uma claim com `evidence_kind=remote_git` e `candidate_sha` igual à base vira integração verified sem recibo, repositório/PR/base/merge, leitura remota ou atestação. A entrada é aceita pelo caminho público `--claims`. O handoff HF-01.md:84 exige que recibos importados sem proveniência permaneçam reported.

Separadamente, um `ProbeObservation(status=ok, validation_mode=simulation, environment=unit-test)` faz operação virar verified. O snapshot nem retém a observação de probe: a reprodução terminou com zero claims e sem `unit-test` serializado. Também se promove toda a operação do item por qualquer HTTP OK, sem escopo da capacidade sondada. Contraria HF-01.md:161 e a limitação explícita de health check.

RCA: verified sem prova adequada → decisões dependem apenas de enum/OK → campos de proveniência/escopo não participam do gate → observação original é descartada → testes cobrem somente falhas HTTP e claim remota stale. Criar evidência atestada distinta da importada; só promover quando origem, dimensão, escopo, ambiente e validade forem compatíveis; persistir IDs e dados sanitizados do probe que justificam a avaliação.

### P1 — HF-01 collect usa claims cujo hash não corresponde à fonte atual

Local: `core/planning/baseline_cli.py:95–103`, `core/planning/baseline_reconcile.py:45–49`.

A reprodução forneceu uma claim com source_hash `a` repetido 64 vezes, enquanto a fonte real tinha SHA-256 `ea048557...f5e03a83f`. Collect retornou 0 e incorporou a claim; verify com root também retornou 0. A aplicação valida o formato do hash, mas nunca sua igualdade com a observação coletada nem a existência do source_id/locator. Um relatório modificado após curadoria pode continuar promovendo uma conclusão antiga indefinidamente.

Esse problema difere de adulterar o snapshot pronto: a própria coleta produz um snapshot inconsistente a partir de entradas aceitas. O teste estático `test_baseline_catalog.py:145` confere apenas os arquivos fixos do repositório no momento do pytest, sem proteger outras coletas ou alterações posteriores. Validar vínculo/hash/status das fontes em todo collect; mismatch deve gerar stale/corruption e impedir uso positivo da claim.

## Outros bugs e diferenças de aceite

### P2 — Contradições no mesmo escopo são silenciosamente descartadas

`core/planning/baseline_reconcile.py:140–152`: claims positivas e negativas para a mesma dimensão/escopo resultaram em `reported` e nenhum source_conflict referente às claims. Só declarações de status geram conflito. `AssessmentDimension.CONTRADICTED` nunca é produzido. Cobrir positivo+negativo no mesmo ambiente/escopo e separar de evidências legitimamente diferentes; a regra está em HF-01.md:58.

### P2 — Filtro de aprovação não compara com o digest do workflow

`spikes/runtime_choice/effect_store.py:371–382`: `record_approval` só procura `decision_id`. Registrar decision-A/workflow-A/release-A e depois decision-B/workflow-A/release-B aceita ambos. Tampouco existe registro do digest esperado antes da primeira decisão. Isso verifica reuso do ID, não a proteção exigida para o R06. HF-02.md:58 e o cenário R06 exigem rejeitar release-B para um run release-A antes de entregar a decisão ao runtime.

O efeito indevido não foi demonstrado porque o adaptador de espera/DBOS e controlador R06 estão declaradamente pendentes; o bug confirmado é a aceitação do digest divergente pelo filtro comum já entregue em HF-02-03. Acrescentar fonte autoritativa do digest esperado e cobrir tanto um novo decision_id inválido como o reenvio idempotente válido. Não atribuir essa proteção ao runtime.

### P2 — Falta de run e erro de store escapam do protocolo estruturado

`spikes/runtime_choice/native_adapter.py:273–287`: `get_latest_run` retorna None para ID desconhecido; acessar `record.status` fora do try gera AttributeError, capturado na reprodução. `driver.py:107–114` não converte esse erro em evento JSONL. Depois da correção do carregamento JSON, observe de run ausente pode encerrar o driver.

Na construção direta do adaptador com `native/orchestrator.sqlite3` sendo um diretório da fixture, o startup também gera `OperationalError: unable to open database file`, sem DriverConfigurationError. A reprodução CLI atual fica mascarada pelo primeiro P1; não é correto afirmar que R09 já ficou validado porque o CLI devolveu CONFIG_INVALID. Converter erros esperados de I/O em códigos sanitizados e verificar ausência de fallback por leitura independente do store.

### P2 — Campos obrigatórios de origem da validação não existem no resultado HF-02

`spikes/runtime_choice/contracts.py:457–472`, `:516–527`: ScenarioResult rejeita `environment_ref`, `validation_mode` e `target_differences` como campos extras; RuntimeComparison só tem environment_ref. A alteração obrigatória de HF-02.md:205 define `real_lab`, `target_environment`, `mock_only`; o enum e os campos não foram implementados em HF-02-02, embora esse ticket seja reportado entregue. A falta impede separar provas de mock e laboratório na comparação futura. É contrato ausente da entrega atual, não alegação de que a rodada DBOS pendente foi executada.

### P2 — Collect retorna sucesso com fonte obrigatória ausente

`core/planning/baseline_cli.py:116`: sempre retorna 0 após escrever outputs. A reprodução com uma fonte required inexistente gerou snapshot parcial/blockers e exit 0. O handoff HF-01.md:72 exige código 2 para fonte obrigatória ausente e 3 para corrupção/grafo; main também converte genericamente erros em 2. Preservar o relatório diagnóstico, mas retornar a categoria contratada e testar os códigos pelo CLI.

### P2 — Parser inventa dependência de outro prefixo

`core/planning/baseline_sources.py:467–486`: ao extrair `_FULL_ID`, o token correspondente permanece no texto usado por `_BARE_ID`. `_parse_markdown_dependencies('DF-11', 'HF')` devolve `['DF-11','HF-11']`. Pode criar bloqueios ou ciclos falsos quando uma tabela HF referencia DF. Remover os spans consumidos e testar dependências entre prefixos, ranges e texto não estrutural.

### P2 — Limites dos probes diferem da especificação

`core/planning/baseline_probes.py:22`, `:50–51`: default é 10 segundos, configurável até 60, e corpo até 2 MiB. A especificação HF-01.md:78 limita a 3 segundos e 64 KiB. Transporte fake retornando 65.537 bytes foi classificado OK. Corrigir os limites máximos e cobrir precisamente 65.536/65.537 bytes e configuração acima de 3 segundos.

## Oráculos e especificações que precisam ser reforçados

- `tests/test_runtime_spike_effects.py:111` chama de congelamento uma asserção de tamanho do hash: qualquer catálogo alterado continua gerando 64 caracteres. Congelar o digest esperado em uma fonte de especificação revisada e adicionar contraprova que altera um resultado esperado e exige rejeição.
- `tests/test_baseline_catalog.py:181` confere valores esperados escritos na própria fixture, sem executá-los contra o pipeline. Os testes de reconciliação separados cobrem apenas parte de A1–A10 e não exigem controles negativos para proveniência/claims conflitantes. Executar fixtures de negócio de ponta a ponta com expected independente e alterá-las deliberadamente para comprovar sensibilidade.
- Toda interface de JSON/CLI deve ter teste real do produtor/consumidor; um objeto Python válido não prova um protocolo serializado. O teste de subprocesso precisa observar readiness, etapa persistida, terminal e erro, sem depender de bool do driver.
- A fronteira de confiança deve constar dos modelos: quem pode atestar, quais metadados são importados sem confiança, que hashes ligam a claim aos bytes, como o probe fica preservado e qual capacidade específica ele prova.
- A definição de readiness precisa ter contraprovas: dados ausentes, fontes trocadas, API divergente, grafos inválidos, SHA desconhecido e assessments adulterados não podem produzir gate verde. Não confundir completude de leitura com validade da conclusão.
- Preservar os gates declaradamente pendentes: HF-02-05/06/07/08, DBOS real, controlador/oracle e ADR não foram tratados como implementados nesta revisão. A decisão arquitetural continua sem evidência suficiente; não substituir esses ensaios por mocks.
