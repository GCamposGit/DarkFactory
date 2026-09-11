# HF-02-03 — serviço externo e catálogo de oráculos

## Identidade

- Ticket: `HF-02-03`, demanda explícita do owner; depende de `HF-02-02`.
- Owner: Codex; branch: `codex/hf-remediation-wave1-luna`.
- Worktree: `C:\dev\DarkFac`.
- SHA-base: `5604fa0b6e5ccb4921b28fd9d2528758947fae9a`.
- SHA final da implementação: `0e53b6aeed766551668edff73bd66ceab0c0a22c`.
- Heartbeat/lease: frente única local; sem lease concorrente e sem processos persistentes ao final.

## Entrega

- `NativeEffectStore` persiste efeitos, aprovações, bindings de sujeito e observações em SQLite próprio sob `<lab-root>/effects/effects.sqlite3`.
- `EffectServer` usa `ThreadingHTTPServer` em loopback, porta dinâmica e limite de 16 KiB por corpo.
- `POST /effects` é idempotente por `operation_key`, rejeita identidade divergente com 409 e suporta `commit_then_disconnect_once` após commit.
- `POST /observations` preserva invocações repetidas e sequência monotônica por workflow.
- Aprovações exigem `bind_approval_subject(workflow_id, payload_digest)` no store confiável; o binding é idempotente, não pode ser redefinido e não há endpoint HTTP para criá-lo. Digest ausente ou divergente é rejeitado antes da entrega.
- `scenarios.json` contém R01–R12 em ordem; o teste fixa o hash dos bytes versionados: `daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8`.

## Oráculos e validação

| Comando | Resultado |
| --- | --- |
| `python -m pytest tests/test_runtime_spike_effects.py -v` | 6 passed, exit 0 |
| `python -m pytest tests/test_runtime_spike_contracts.py tests/test_runtime_spike_effects.py tests/test_runtime_spike_native.py tests/test_runtime_recovery.py -v` | 23 passed, exit 0 |
| `python -m compileall -q spikes\runtime_choice tests\test_runtime_spike_effects.py` | pass, exit 0 |
| `git diff --check` | pass |
| `python core/harness/runner.py --quick` | 532 collected, 530 passed, 2 skipped, `[HARNESS_PASS]`, exit 0 |
| `python -m pytest tests -v` | 532 collected, 530 passed, 2 skipped, exit 0 |

Os dois skips são preexistentes e opt-in: ensaio live de áudio e criação de symlink indisponível neste Windows. A revisão adversarial reproduziu o defeito de aprovação por novo `decision_id`; a contraprova e o fluxo válido agora estão na suíte focal.

## Manifesto SHA-256 dos paths do ticket

```text
b78b0a341b4859c11bebaa782d8c64c3b4b576f62cdc61ec867dea9b9d9964ed  spikes/runtime_choice/effect_store.py
f5c91c397dc8fd7561c490b31fa66ebc392fda80116399f98ddd71a673acbca8  spikes/runtime_choice/effect_server.py
daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8  spikes/runtime_choice/scenarios.json
a163d613542ec7b08d28f9a2057457f8b6b35c52b0e333bff1ee9997fccc121f  tests/test_runtime_spike_effects.py
```

## Estado de entrega

O commit local foi criado seletivamente; alterações e arquivos não relacionados já existentes no worktree foram preservados. A publicação remota está bloqueada neste ambiente: `git fetch origin main` não conseguiu criar `.git/FETCH_HEAD` por permissão, e `git ls-remote origin refs/heads/main` falhou por conectividade com `github.com:443`. Não há PR, checks, merge ou SHA remoto verificados para este ticket.
