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
| `evals/e2e/cli.py` | `manifest`, `verify`, `replay`. |

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

## Como plugar a execucao real (fora desta entrega)

Esta entrega nao chama modelos nem a linha de producao. Para medir de verdade:

1. Para cada caso, entregue `agent_view(case)` a linha como demanda (nunca `spec` nem `journey`).
2. Registre eventos conforme a linha trabalha (falha injetada, deteccao, recuperacao, pedido
   humano) com tempo relativo ao inicio e o custo total.
3. Suba o produto entregue e implemente um `OracleDriver` (`execute(step) -> Observation`):
   `cli` roda o ponto de entrada com `args`/`stdin` (e le `read_file` no diretorio de trabalho);
   `http` envia `method/path/json_body` ao `base_url` (use `signed_body(step)` para o HMAC);
   `chat` envia `message` pelo adaptador como `user`. Normalize CRLF para LF na saida.
   `evaluate_journey(case, driver)` devolve os `JourneyResult` da trajetoria.
4. Grave a trajetoria como JSON (`Trajectory`) e rode `replay`. Faltas injetadas por
   `recovery.fault` devem ser registradas como `fault_injected` pelo orquestrador do experimento.
5. Compare sistemas com `compare_reports`.
