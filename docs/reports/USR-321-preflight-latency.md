# USR-321 (fatia 1): latencia do preflight de cota e wake

## Medicao (2026-10-21, Notebook, relogio `time.monotonic()`)

Cada sonda agora loga `probe provider=... outcome=... duration_s=...` (`core/usage/probe_deadline.py`);
o wake loga `wake preflight probe ... duration_s` e `wake email duration_s`.

| Sonda | Duracao | Resultado |
|---|---|---|
| anthropic (Claude) | 2,8 s (repetivel) | `probe_unavailable` (fail-closed) |
| openai (Codex) | 3,3 s; 0,0 s com cache | healthy 40% |
| xai (Grok) | 0,9 s | healthy 23,3% |
| google (Antigravity) | 10,0 s ate o deadline | `probe_timeout` (antes: sem limite total) |
| `pick('development', ...)` completo | 2,8 s (so Claude re-sonda) | elegeu codex |
| grokbox `/health` | 0,3 s (host online) | ok |

## Causa observada

Nao ha deadline total: cada adaptador soma timeouts internos sequenciais (CLI 8-15 s mais HTTP de 2-5 s
em `core/usage/adapters.py`: Codex app-server 15 s, Claude CLI 12 s + HTTP 4 s x2, Gemini varias chamadas
2-4 s), de modo que uma sonda lenta ou presa no pior caso passa de 30 s sem erro estruturado; o wake pode
somar ainda a espera de ate 300 s (`DARKFAC_GROKBOX_WAKE_WAIT_SEC`) e o envio de e-mail sem limite.
Lentidao severa (>10 s) reproduzida com o provedor google; as demais ficaram em 1-3 s. A demora de minutos
relatada nao foi reproduzida com a rede saudavel, so o excesso sem limite do Google.

## Correcao (b)

- `run_with_deadline` (env `DARKFAC_PROBE_DEADLINE_SEC`, padrao 10 s) envolve `adapter.inspect` em
  `_quota_headroom_diagnosis`; estouro mata a arvore de processos filha e levanta `ProbeTimeout`
  (`{"error":"probe_timeout","provider","duration_s","deadline_s"}`).
- O roteador devolve `(None, "probe_timeout")`: cota desconhecida, fail-closed, harness nao eleito.
- Probe e e-mail do wake (`core/line/grokbox_wake.py`) usam o mesmo deadline.
- Testes: `tests/test_usr321_probe_deadline.py`.
