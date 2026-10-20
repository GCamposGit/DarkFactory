# Proposta: ids de modelo dos arquivos protegidos passam a referenciar o registro (USR-185 fatia 3)

Os arquivos abaixo sao protegidos por `core/orchestrator/guard.py`; a fabrica NAO os altera. Este documento e o
patch ao lado (`docs/proposals/USR-185-F3-protected-registry.patch`) trazem a mudanca; o owner publica uma vez (OA-046).

| Arquivo | Mudanca |
|---|---|
| `core/harness/test_subagent.py` | 9 ids literais (`openai/gpt-5-6-luna-high`, `xai/grok-4.6`, `anthropic/claude-3.7-sonnet`, `anthropic/claude-haiku-5-5`, `claude-3-7-sonnet`, `claude-haiku-5-5` nos snippets) viram `gateway_id(...)`/`id_of(...)`/`resolve("review.grok")` de `core.models.registry` |
| `core/harness/remote_worker.py` | defaults `gpt-5.6-sol`, `grok-4.6`, `claude-sonnet`, `deepseek-coder` (8 ocorrencias) viram `gateway_id("sol")`, `resolve("review.grok")`, `gateway_id("sonnet")`, `id_of("coder-legacy")` |
| `core/harness/suite_lock.py` | os 2 rotulos de harness `claude-code` (nao e id de modelo) recebem `# census: wire-id` |
| `AGENTS.md`, `FACTORY_RULES.md`, `CLAUDE.md` | `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` viram os aliases `sonnet`, `opus`, `haiku` mais a referencia a `.factory/config/model_registry.json` |

## 1. Por que

O USR-168 (adotar Haiku 5.5) exigiu o owner editar a mao estes arquivos. Com o registro como unica fonte de ids,
trocar ou adicionar modelo nao toca mais nos protegidos. Apos o push, `python scripts/model_id_census.py --verbose` nao
lista nenhum destes seis arquivos.

## 2. Comportamento identico

- Todos os valores devolvidos sao os mesmos strings de antes. Para isso a entrega (nao protegida, vai no PR desta fatia e precisa estar na main antes do
  patch) acrescentou ao registro `gateway_ids`: `claude-sonnet-5-5` -> `claude-sonnet`, `claude-haiku-5-5` ->
  `anthropic/claude-haiku-5-5`, `gpt-6-luna` -> `openai/gpt-5-6-luna-high`, `gpt-sol` -> `gpt-5.6-sol`; a entrada
  `deepseek-coder` (deprecated, alias `coder-legacy`) e o alias `legacy-sonnet`; o fallback embutido espelha tudo.
- Comparacao antes/depois: `get_harness_test_runner_spec` para os 16 combos (harness x prefer_local x deep_rca) com
  `model_dump()` identico (0 diferencas); `gateway_id("sol")=gpt-5.6-sol`, `gateway_id("sonnet")=claude-sonnet`,
  `resolve("review.grok")=grok-4.6`, `id_of("coder-legacy")=deepseek-coder`.
- A semantica de arquivo ausente/invalido continua fail-closed (fallback embutido do registro).

## 3. Invariantes preservadas

Nenhuma regra muda de texto: piso de cota de 15.0%, revisao em familia diferente do implementador (o revisor nunca e
Haiku), Haiku proibido em caminhos protegidos/tags de risco/estagios planning, grill, review e integration, e
`forbidden_autonomous_models` (Fable, Astra) continuam explicitos nos protegidos. Alem disso o teste
`tests/test_model_registry_invariants_usr185.py` verifica piso, banimento de fable/astra e revisor de outra familia
contra o registro (passa na main atual, sem o patch).

## 4. Testes rodados

Com o patch aplicado na worktree: `tests/test_model_registry.py`, `test_model_registry_readers_usr185.py`,
`test_suite_lock.py`, `test_suite_lock_stalled.py`, `test_remote_worker_multi_harness.py`,
`test_remote_codex_harness.py`, `test_implementar_execu_o_de_te.py`, `test_remote_worker_restart.py`,
`test_focal_timeout_partial_output.py`, `test_pytest_suite_lock_policy.py`, `test_remote_signal_killed.py`
(101 passed, 1 skipped POSIX) e `test_worker_onprem_test_subagent.py`, `test_remote_dispatch.py`,
`test_remote_restart_retry.py`, `test_gate_lock_timing.py`, `test_process_identity_usr317.py` (78 passed, 1 skipped
live). Portao completo NAO rodado aqui: roda o owner no passo da OA-046.

## 5. Como aplicar

Passo a passo na OA-046 (DarkHub). Resumo, em PowerShell:

```powershell
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\usr-185-f3-protegido origin/main --detach
cd C:\dev\DarkFac\.worktrees\usr-185-f3-protegido
git apply --check docs\proposals\USR-185-F3-protected-registry.patch
git apply docs\proposals\USR-185-F3-protected-registry.patch
git add AGENTS.md CLAUDE.md FACTORY_RULES.md core/harness/remote_worker.py core/harness/suite_lock.py core/harness/test_subagent.py
git commit -m "refactor(harness): ids de modelo dos protegidos passam a referenciar o registro [USR-185]"
$env:DARKFAC_TEST_WORKERS='http://100.77.130.5:8080'; python core\harness\runner.py --quick --remote-required
git fetch origin
git rebase origin/main
git push origin HEAD:main
```

Pre-requisito: o PR desta fatia (registro com `gateway_ids`) precisa estar na `origin/main`; sem ele o patch quebra
(`gateway_id("sol")` etc. nao existem). Commite SOMENTE os 6 caminhos acima. Se outro patch pendente (por exemplo
GROKBOX) tocar `core/harness/test_subagent.py`, os hunks nao se sobrepoem; aplique um por vez.
