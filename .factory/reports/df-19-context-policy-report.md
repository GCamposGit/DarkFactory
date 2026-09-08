# DF-19 — Context policy and learning promotion lifecycle

## Identidade e escopo

- Ticket: `DF-19` (`user-demand`)
- Owner: Codex
- Branch: `codex/df-19-context-policy`
- Worktree: `C:\\dev\\DarkFac`
- SHA-base: `104e331e4305c6267ee31304bd30fc9556178e50`
- SHA-final (implementation commit): `9a4dee0c6da35817be0e2cc845708797e69da9a0`
- Experimento Canaletto: fora do escopo; nenhum arquivo do experimento foi alterado.

## Entrega

- `core/learning/promotion.py`: contrato fechado `LearningCandidate` e motor de promoção com avaliação versionada, runs empíricos, persistência atômica e rollback para `RETIRED`.
- `core/orchestrator/context.py`: contratos `TaskContext` e `FileReferenceSpec`, seleção de regras `ACTIVE` por escopo, resumo bounded, referências estruturadas e progresso durável de checkpoints.
- `tests/test_context_policy.py`: 12 testes focais cobrindo schema, gates de promoção, divergência de avaliação, ativação, rollback, persistência, isolamento de regras e limites determinísticos do contexto.
- `docs/HARNESS_INTEROP.md`: protocolo de contexto bounded e governança de aprendizado entre harnesses.

## Validação

| Comando | Resultado | Exit code |
| --- | --- | ---: |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]` | 0 |
| `python -m pytest tests/test_context_policy.py tests/test_learning_engine.py tests/test_agent_evals.py tests/test_orchestrator_state.py tests/test_runtime_recovery.py -v` | 39 passed | 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`; 360 passed, 1 skipped; 361 descobertos | 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 360 passed, 1 skipped; 361 descobertos | 0 |
| `git diff --check` | sem erros nos arquivos do ticket | 0 |

## RCA / decisões

- `rca_ctx_01`: o limite declarado de contexto não era aplicado ao resumo; agora o texto operacional é truncado de forma determinística por aproximação de quatro caracteres por token.
- `rca_prom_01`: uma falha de reavaliação não podia manter regra ativa; agora falha aposenta regra ativa e promoção exige histórico persistido de avaliação aprovada na mesma versão.
- Rollback só aceita `ACTIVE`, exige justificativa e restaura o estado em memória se a persistência falhar.

## Estado residual

O working tree já continha alterações não relacionadas ao DF-19 antes desta frente e elas foram preservadas fora do commit:

- `.factory/infra/inventory.json`
- `.factory/infra/decisions/ADR-002-vps-and-paas-orchestration.md`
- `core/infra/inventory.py`
- `tests/test_roadmap_scale.py`
- `.factory/infra/roadmap.json`
- `.factory/infra/roadmap.md`

O SHA da branch publicada e a evidência de entrega remota serão registrados no handoff final.
