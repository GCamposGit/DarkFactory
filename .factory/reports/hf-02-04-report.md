# HF-02-04 — adaptador da baseline atual

## Identidade

- Ticket: `HF-02-04`; depende do serviço de efeitos do `HF-02-03`.
- Branch: `codex/hf-remediation-wave1-luna`.
- Worktree: `C:\dev\DarkFac`.
- SHA-base local: `0f3e595501a664164a5f1cd38f40a1e87c744683`.
- SHA da implementação: `e474ae3f025d6de312a684115bc86120f95244b3`.
- Worktree compartilhado: alterações não relacionadas foram preservadas e ficaram fora do commit.

## Entrega

- `NativeAdapter` usa somente as APIs existentes de `core.orchestrator`, com store exclusivo em `<lab-root>\native\orchestrator.sqlite3`.
- A execução mantém cada workflow em thread própria, publica readiness/etapas pelo protocolo JSONL e usa o serviço de efeitos independente do `HF-02-03`.
- O driver aceita configuração por `--config` ou `HF02_CONFIG`, importa o adapter lazy e mantém os gaps da baseline explícitos: wait durável, cancelamento antes do próximo step, intake deduplicado, isolamento de versão e concorrência limitada retornam `CAPABILITY_UNSUPPORTED`.
- Recuperação de execução sem lease funciona pelo claim/reclaim do runtime nativo.

## Revisão adversarial e CR-14

- O teste negativo reproduziu o defeito original em que run ausente causava `AttributeError` e o erro de store era confundido com `RUN_NOT_FOUND`.
- `observe` agora distingue ausência real (`RUN_NOT_FOUND`), erro SQLite (`STORE_UNAVAILABLE`) e estado interno inválido (`NATIVE_RUNTIME_ERROR`).
- Store inacessível no startup gera apenas `STORE_UNAVAILABLE` no stderr, saída não zero, nenhum readiness e nenhum fallback para outro store/runtime.
- A implementação não converte falhas internas em `CAPABILITY_UNSUPPORTED`.

## Validação

| Comando | Resultado |
| --- | --- |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]` |
| `python -m pytest tests/test_runtime_spike_native.py tests/test_runtime_recovery.py -v` | 15 passed, exit 0 |
| `python core/harness/runner.py --quick` | 549 coletados, 547 passaram, 2 skips, `[HARNESS_PASS]`, exit 0 |
| `python -m pytest tests -v` | 549 coletados, 547 passed, 2 skipped, exit 0 |
| `git diff --cached --check` | pass antes do commit |

Os dois skips são opt-in/preexistentes: ensaio live de áudio e criação de symlink indisponível neste Windows. A suíte emite um warning não bloqueante porque o `.pytest_cache` do worktree não é gravável.

## Manifesto SHA-256 dos paths do ticket

```text
5AFFF784A7E0923E55F8057DF1ABC888039633558992AD57E498166D54660D67  spikes/runtime_choice/native_adapter.py
438645DC5535CF61F33405CD50646E2495570E8EE214B5BE02FD56CC224199A6  spikes/runtime_choice/driver.py
E17CF286DA7EACC4CB54CEB1582362A30B4CF6D9E24DEF4161BDAF27792C0A70  tests/test_runtime_spike_native.py
```

## Estado de entrega

O commit está criado localmente. Este ticket ainda não possui PR, checks remotos ou merge verificados. O `HF-02-03` também permanece publicado na branch remota, porém ainda sem PR mesclado; portanto a sequência correta de entrega remota é concluir essa dependência antes de promover o `HF-02-04`.
