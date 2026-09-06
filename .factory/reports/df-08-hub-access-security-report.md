# DF-08 — DarkHub access security

- Ticket: `DF-08`
- Task ID / owner: `/root/df08_preflight`
- Lease: fencing token `3`, expires `2026-09-06T12:55:35Z`
- Branch: `codex/df-08-hub-access-security`
- Worktree / cwd: `C:\dev\DarkFac_worktrees\df08`
- SHA-base: `a4fd4817fe6f911d70bc9a45187d11ac7357940e`
- SHA final: commit seletivo que contém este relatório; SHA concretizado no contrato WT-08

## Entrega

- Host headers são restringidos a loopback/testserver ou à allowlist explícita.
- Origens de navegador são limitadas a loopback com porta válida ou à allowlist explícita.
- A página inicial estabelece uma sessão local HttpOnly/SameSite; origem presente ou token configurado exige sessão válida.
- Health probes aceitam somente serviço cadastrado e URL correspondente, com esquemas HTTP(S), sem credenciais ou fragmento.
- Resolução DNS separa serviços loopback de destinos públicos e bloqueia endereços privados, reservados e mistos.
- Cada redirect é revalidado, e um probe HTTPS não pode sofrer downgrade para HTTP.

## Arquivos possuídos e alterados

- `hub/backend/main.py`
- `hub/backend/api.py`
- `hub/backend/service.py`
- `tests/test_hub_access.py`
- `.factory/reports/df-08-hub-access-security-report.md`

## Evidência de validação

| Comando | Resultado | Exit code |
|---|---:|---:|
| `python -m pytest tests/test_hub_access.py -q` | 4 passed | 0 |
| `python -m pytest tests/test_hub_access.py tests/test_hub.py -q` | 14 passed | 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`; 207 passed, 1 skipped, 208 coletados | 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 207 passed, 1 skipped | 0 |
| `git diff --check` | sem erros | 0 |

O único skip é o ensaio live de áudio/GPU, desabilitado por padrão. O estágio vermelho
test-first falhou na coleta pela ausência esperada de `ProbeTargetError` antes da
implementação.

## Estado residual

`.factory/usage/` foi regenerado pela suíte e não pertence ao ticket; permanece
untracked, explicitamente excluído do commit. O commit seletivo contém apenas os cinco
caminhos de ownership acima.

Heartbeat final antes do commit: `2026-09-06T11:03:03Z`, fase `REPORT/COMMIT`,
HEAD-base `a4fd4817fe6f911d70bc9a45187d11ac7357940e`.
