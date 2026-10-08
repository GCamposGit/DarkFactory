# Avaliacao E2E reservada da fabrica (USR-133)

O corpus DF-18 (`evals/tasks.jsonl`, 24 tarefas de dois arquivos) continua valendo
como smoke de patches. O pacote `evals/e2e/` mede o **sistema completo**: produto
inteiro, jornada de aceitacao independente da implementacao e recuperacao de falha.

## Componentes

| Arquivo | Papel |
| --- | --- |
| `evals/e2e/models.py` | Contratos pydantic v2: caso, oraculo, manifesto, trajetoria, hashes SHA-256 canonicos. |
| `evals/e2e/corpus/*.json` | Corpus inicial reservado: 6 produtos (API HTTP, CLI, site estatico, bot, batch, webhook). |
| `evals/e2e/manifest.json` | Manifesto versionado: so `case_id`, `sha256`, visibilidade e tipo. Nunca conteudo. |
| `evals/e2e/corpus.py` | Carregador/validador, manifesto, `agent_view`. |
| `evals/e2e/oracle.py` | Passos executaveis (`cli`, `http`, `chat`), `check_expectation`, `evaluate_journey`, protocolo `OracleDriver`. |
| `evals/e2e/leakage.py` | `LeakScanner`: canario, n-gramas do SPEC e literais do oraculo. |
| `evals/e2e/replay.py` | Runner dry/replay, relatorio, recibos, `verify_report`, `compare_reports`. |
| `evals/e2e/driver.py` | `LineRunner` (o que a linha ve), `ControlStoreLineRunner` (intake real num `ControlStore` injetado), `load_runner`. |
| `evals/e2e/orchestrator.py` | `E2EOrchestrator`: percorre o corpus, injeta `recovery.fault` via `FaultInjector`, grava trajetorias e o relatorio. |
| `evals/e2e/cli.py` | `manifest`, `verify`, `replay`, `run`, `compare`. |

## Caso do corpus

Cada caso tem: `brief` (unico texto entregue ao agente avaliado), `spec` curto
(somente avaliador), `journey` (passos de oraculo que falam apenas com a interface
publica: argumentos de CLI, requisicoes HTTP, mensagens de chat), `recovery` (falha
injetada, fase `build` ou `runtime`, prazo de recuperacao), orcamentos de tempo e
custo e um `canary` unico.

O validador rejeita: oraculo que cita diretorio ou arquivo de implementacao
(`src/`, `.py`, `.js`, caminho absoluto, `..`); ids, produtos, canarios ou titulos
repetidos; dois casos com o mesmo tipo de produto; dois casos cuja jornada se
sobrepoe em mais de 50% dos passos; dois casos com o mesmo conteudo de arquivo semente;
manifesto que nao bate com o conteudo (deriva de hash).

## Trajetorias e metricas

Uma trajetoria (JSON, uma por execucao) liga `case_id` + `case_sha256` a um
`system_id`, `final_state`, duracao, custo, resultado por passo da jornada e eventos
(`agent_step`, `tool_call`, `fault_injected`, `fault_detected`, `recovered`,
`human_intervention`). O replay calcula, sem modelo nem rede:

- **Sucesso E2E**: entregue e todos os passos da jornada do caso passaram (passo ausente = falha).
- **Sucesso autonomo**: sucesso sem nenhum `human_intervention`.
- **Intervencao humana**: total, taxa de execucoes com intervencao.
- **Tempo**: media, mediana, p90. **Custo**: total, medio, por sucesso, execucoes acima do orcamento.
- **Recuperacao**: `recovered` (falha injetada, detectada, recuperada no prazo, jornada ok, sem humano),
  `human_assisted`, `failed`, `not_exercised`. `recovery_rate = recovered / exercised`.
- **Cobertura**: casos do corpus com ao menos uma execucao valida; ausentes ficam em `missing_cases` (id + hash).

Execucoes invalidas (caso desconhecido, hash do caso defasado, passo desconhecido,
`trajectory_id` repetido, vazamento) entram como recibos **rejeitados e falhos**: nunca
inflam as taxas.

## Recibos e comparacao

Cada execucao gera um recibo com `case_sha256`, `trajectory_sha256` e `receipt_sha256`;
o relatorio tem `manifest_sha256` e `report_sha256`. A saida e deterministica (mesma
entrada, mesmos bytes). `verify_report` recalcula todos os hashes e
`compare_reports(a, b)` so compara relatorios do mesmo manifesto, mostrando deltas de
metricas e casos que mudaram, sempre por id + hash.

## Anti-vazamento

- Casos reservados ficam fora do que o agente ve: `agent_view(case)` devolve so `case_id` e `brief`.
- Relatorios, recibos, achados de vazamento, CLI e manifesto referenciam apenas id + hash;
  `LeakFinding` nunca carrega o trecho encontrado.
- O replay varre cada trajetoria (canario, n-gramas de 8 palavras do SPEC fora do brief, literais
  do oraculo com 12+ caracteres fora do brief) e rejeita as que reproduzem conteudo reservado.
  O relatorio e varrido de novo antes de selado (falha fechada com `LeakError`).
- Em producao, mantenha os JSON dos casos reservados fora da arvore visivel ao agente (volume ou
  repositorio privado do avaliador) e versione so `manifest.json`. `load_corpus(dir, manifest_path=...)`
  aceita qualquer diretorio. Os 6 casos deste repositorio sao o corpus semente; ao promover um caso
  a publico, troque `visibility` (o hash muda e o manifesto precisa ser regerado).
- Ao editar um caso, regenere o manifesto com `python -m evals.e2e.cli manifest --write --corpus-version X.Y.Z`
  (a partir de `C:\dev\DarkFac`) e suba `corpus_version` quando o conjunto mudar.

## Comandos

```powershell
cd C:\dev\DarkFac
python -m evals.e2e.cli verify
python -m evals.e2e.cli replay --trajectories C:\dev\DarkFac\.factory\e2e_runs\sysA --system sysA --out C:\dev\DarkFac\.factory\e2e_runs\sysA-report.json
python -m pytest tests/test_factory_e2e_eval.py -q
```

## Execucao real: driver e orquestrador (USR-160)

O framework agora tem as duas pontas que faltavam. Nenhuma delas e executada pelos testes
nem por padrao: a execucao contra a linha so acontece quando o chamador instancia um runner
explicitamente.

```
Orquestrador (lado do avaliador)            LineRunner (lado da fabrica)
  ve: case completo, spec, jornada,          ve: AgentView (case_id + brief)
      recovery, hashes                           e FaultAction (kill_worker, ...)
  agent_view(case) ----------------------->  submit_case(view) -> run_ref
  poll(run_ref) <-----------------------     RunSnapshot(phase, cost, eventos novos)
  FaultInjector.inject(...) -------------->  apply_fault(run_ref, action) -> bool
  OracleDriver (jornada) <-----------------  product_driver(run_ref)
  grava Trajectory JSON  --> replay --> relatorio com recibos
```

### `LineRunner` (`evals/e2e/driver.py`)

`submit_case(view)`, `poll(run_ref)`, `apply_fault(run_ref, action)`, `cancel(run_ref)` e
`product_driver(run_ref)`. O runner nunca recebe `E2ECase`: nem spec, nem jornada, nem a
descricao do cenario de recuperacao. `poll` devolve a fase (`pending`, `running`,
`delivered`, `failed`, `cancelled`), o custo acumulado e **so os eventos novos**
(`agent_step`, `tool_call`, `fault_detected`, `recovered`, `human_intervention`). O orquestrador
carimba tempo e ordem; por isso o tempo de recuperacao tem a granularidade do `--poll-interval`.
Um runner nao consegue forjar `fault_injected`: esse evento pertence ao orquestrador e e ignorado
se o runner o emitir.

`ControlStoreLineRunner(store, driver_factory=...)` submete pelo mesmo intake publico da linha
(`AutonomousIntakeService.accept`, canal `e2e-eval`) com payload feito so de `case_id` (titulo) e
`brief`. Regras de seguranca:

- o `store` e **obrigatorio e injetado**; o runner nunca abre store nem le `DARKFAC_HF02_DATABASE_URL`,
  `DARKHUB_LINE_DATABASE_URL` ou `DARKHUB_CONTROL_DATABASE_URL`;
- recusa store em `mock_mode` e store Postgres sem `allow_remote_store=True`;
- fase, passos, pedidos humanos (`waiting_human`), deteccao (retry/replan/falha/cancelamento depois da
  falha) e recuperacao (novo estagio concluido ou entrega) sao derivados dos jobs do run;
- `driver_factory(run_id)` devolve o `OracleDriver` do produto entregue (a linha entrega codigo; como
  subi-lo e decisao de quem monta o experimento);
- falhas que dependem de infraestrutura (matar worker, cortar rede, ...) entram por `fault_hooks`
  (`FaultAction -> callable(run_id)`); sem hook, `apply_fault` devolve `False`. `CANCEL_RUN` e embutida:
  cancela o run e reenvia o mesmo brief como a proxima tentativa (como o retry do dono).

### `E2EOrchestrator` (`evals/e2e/orchestrator.py`)

Para cada caso: `agent_view` -> `submit_case` -> `poll` ate estado terminal ou ate `time_budget_seconds`
(timeout cancela o run). Faltas `build` sao injetadas quando o run chega a `running`; faltas `runtime`
depois da entrega, logo apos o passo `inject_after_step` da jornada (que o orquestrador executa com
`evaluate_journey(..., after_step=...)`), esperando o `recovered` dentro de `max_recovery_seconds`.
O `FaultInjector` e injetavel; o `ActionFaultInjector` padrao mapeia `FaultKind -> FaultAction`
(`worker_crash`/`process_crash` -> `kill_worker`, `provider_outage`/`dependency_unavailable` ->
`cut_network`, `merge_conflict` -> `inject_merge_conflict`, `flaky_gate` -> `fail_gate_once`,
`corrupted_state` -> `corrupt_state`) e aceite um mapa proprio.

`fault_injected` so e gravado quando o injetor confirma que aplicou a falha. Se nao aplicou, a
trajetoria ganha `notes: fault_not_applied:...` e o replay a classifica como `not_exercised` (nunca
infla `recovery_rate`). Erros do runner viram trajetoria `error` (so o tipo da excecao em `notes`),
sem derrubar o restante do corpus. Intervencao humana e apenas registrada (`human_intervention`);
o orquestrador nunca responde por um humano.

### Comandos

```powershell
cd C:\dev\DarkFac
# 1) escreva um modulo seu com uma fabrica sem argumentos que devolve um LineRunner
#    (ex.: ControlStoreLineRunner(store_sqlite_temporario_ou_o_que_voce_decidir, driver_factory=...))
python -m evals.e2e run --runner meu_pacote.meu_runner:build --system sysA --out C:\dev\DarkFac\.factory\e2e_runs\sysA
python -m evals.e2e run --runner meu_pacote.meu_runner:build --system sysB --out C:\dev\DarkFac\.factory\e2e_runs\sysB --cases E2E-CLI-UNITS --repeat 3
python -m evals.e2e compare --base C:\dev\DarkFac\.factory\e2e_runs\sysA\report.json --candidate C:\dev\DarkFac\.factory\e2e_runs\sysB\report.json
python -m pytest tests/test_factory_e2e_driver.py -q
```

`run` falha fechada: sem `--runner` (ou com spec que nao e `modulo:fabrica`, nao importa, ou nao
implementa `LineRunner`) sai com codigo 2 sem criar nada; `--out` precisa estar vazio (nao sobrescreve
evidencia). `--no-faults` faz uma execucao de linha de base sem injecao. Saida: `<out>/trajectories/*.json`
(formato `Trajectory`, o mesmo que `replay` consome) e `<out>/report.json` (relatorio selado com recibos).
`compare` usa `compare_reports`: so compara relatorios do mesmo manifesto.

### O que continua fora desta entrega

Nenhuma execucao real foi feita e nenhum modelo pago foi chamado. Ainda cabe ao operador: escolher o
store/ambiente da linha, escrever os `fault_hooks` reais (matar worker, cortar rede) e o `driver_factory`
que sobe o produto entregue. Em testes, use sempre um `SQLiteControlStore` temporario.
