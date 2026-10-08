# USR-158 - Aposentadoria do patch obsoleto `usr-143-harness-core.patch`

Status: decisao registrada. Nenhum caminho protegido foi tocado e nenhum patch novo foi gerado.

## 1. Contexto

`.factory/patches/usr-143-harness-core.patch` foi a proposta do USR-143 para `core/harness/` (protegido por `guard.py`). O USR-143 foi concluido no PR #169 sem esse patch. O owner aplicou depois, no commit `afac42b`, o patch `docs/proposals/USR-141-147-harness-resilience.patch`, que mexe nos mesmos trechos de `suite_lock.py`, `remote_dispatch.py` e `runner.py`. Os dois conflitam e nunca devem ser aplicados juntos (ver secao 5 de `docs/proposals/USR-141-147-harness-resilience.md`). Deixar o antigo no repositorio era uma armadilha: o stall detector dele encerra o dono por padrao e olha so o CPU do processo raiz.

Decisao: o arquivo `.factory/patches/usr-143-harness-core.patch` foi removido (`git rm`). O historico git preserva o conteudo.

## 2. Tabela portar / descartar

Comparacao do patch antigo com `core/harness/` em `origin/main` (c0aab80):

| Item do patch antigo | Estado no main | Decisao |
|---|---|---|
| `probe_timeout_sec()` e env `DARKFAC_REMOTE_PROBE_TIMEOUT_SEC` | Substituido por `health_probe_timeout_sec()` com `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` | Descartado |
| Lock de suite tomado antes do `execute()` no ramo `no_cache` de `run_with_cache` | Ja presente (secao 4 do `.md` do USR-141/147) | Descartado (ja entregue) |
| Stall detector em `suite_lock.py` (CPU so do processo raiz, encerra por padrao, 600 s) | Substituido por `_StallWatch`: amostra a arvore de processos, so avisa por padrao, encerra apenas com `DARKFAC_SUITE_LOCK_REAP_STALLED=1` | Descartado |
| Campo `heartbeat` no sidecar do lock | Nao existe | Descartado |
| Limpeza do sidecar de pid morto (`_check_and_release_stalled_holder`, caso 1) | Nao existe | Descartado |
| `last_probe_failure_reason` e motivo no print `unreachable` | Nao existe | Candidato a porte futuro (secao 4) |

## 3. Justificativa de cada descarte

- Env do probe: dois nomes para a mesma coisa seriam dois contratos de configuracao. O main ja tem `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC`, validado (rejeita vazio, `nan`, `inf`, negativo e zero) e coberto por `tests/test_remote_dispatch_resilience.py`.
- Lock antes do `execute()`: identico ao que o main ja faz; portar de novo geraria conflito.
- Stall detector: a versao antiga mede o CPU do processo raiz do dono. O runner dono fica ocioso enquanto o pytest filho trabalha, entao uma suite saudavel parece parada, e o patch antigo a encerrava por padrao. A versao do main olha a arvore inteira, exige janela de amostragem e deixa o encerramento desligado por padrao.
- `heartbeat`: o patch antigo gravava o campo uma vez e nunca o atualizava nem lia; era um campo morto.
- Limpeza do sidecar de pid morto: o SO libera o lock quando o processo morre, e o proximo a adquirir o slot sobrescreve o sidecar (`_write_sidecar`); `release()` apaga o sidecar no caminho normal. Um sidecar velho de slot livre nao bloqueia ninguem. O ramo do patch antigo so rodava com o lock ja ocupado (ou seja, havia outro dono vivo que ainda nao gravara o sidecar), entao apagar o arquivo nao trazia beneficio e abria uma corrida. O `.md` do USR-141/147 ja registra que a unica perda e a linha de log de "lock orfao liberado", um follow-up trivial se o owner quiser.

## 4. O que o owner poderia portar depois: `last_probe_failure_reason`

Nao ha patch nem codigo aqui, de proposito: gerar e validar esse patch exigiria editar `core/harness/` (protegido) e nao pode ser feito por agente. Descricao do que seria portado, em ordem:

1. Em `core/harness/remote_dispatch.py`, uma variavel de modulo `_last_probe_failure_reason: str | None`, zerada no inicio de `probe_health`, e uma funcao publica `last_probe_failure_reason()` que a devolve.
2. Em `probe_health`, registrar o motivo antes de devolver `None`: `HTTP <codigo>` para resposta nao-200 e para `HTTPError`; `network error: <reason>` para `URLError`; `<TipoDaExcecao>: <mensagem>` para qualquer outra excecao (incluindo timeout). A funcao continua sem levantar excecao.
3. Em `maybe_dispatch_remote`, o print `[REMOTE] <url> unreachable (...)` passa a incluir o motivo, mantendo o texto atual como prefixo (`worker offline or port blocked: <motivo>`), para que as asserções de `tests/line/test_line_unblock.py` e qualquer parser do texto antigo continuem validos.
4. Testes novos em arquivo novo (sem tocar `tests/test_harness_contract.py` nem os demais protegidos): sonda contra porta fechada registra `network error`, resposta 503 registra `HTTP 503`, sonda bem-sucedida zera o motivo, e o print de `maybe_dispatch_remote` contem o motivo. Lembrar que `tests/conftest.py` troca `urllib.request.urlopen` por um stub offline; testes que falam com loopback devem religar um opener real, como faz `tests/test_remote_dispatch_resilience.py`.

Valor: diagnostico mais claro quando o worker aparece "offline" (timeout vs recusa vs HTTP 5xx). Risco: baixo. Prioridade: baixa; nao ha ticket aberto para isso (a regra de nao abrir tickets novos foi respeitada nesta entrega).

## 5. Referencias remanescentes ao caminho antigo

- `docs/proposals/USR-141-147-harness-resilience.md` (secoes 4 e 5): referencias intencionais, descrevem a relacao historica.
- `.factory/reports/usr-143-notebook-suite-lock-timeout-report.md`: recebeu nota apontando para este documento e para `afac42b`.
- `.factory/demands/demands.json` (USR-143 e USR-158): historico da demanda, nao editado.
- Nenhum script, teste ou workflow referencia o caminho (verificado com `grep -rn usr-143-harness-core`).
