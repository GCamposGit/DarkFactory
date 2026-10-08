# Proposta de texto para AGENTS.md, FACTORY_RULES.md e core/harness/test_subagent.py: roteamento de subagentes (Haiku 5.5, USR-168)

`AGENTS.md`, `FACTORY_RULES.md` e `core/harness/*` sao protegidos por `core/orchestrator/guard.py` ("These files can ONLY be modified by a direct human commit"). Por isso a fabrica NAO os edita: este arquivo guarda o texto exato para o owner aplicar num commit proprio. A mesma regra ja esta em vigor em `CLAUDE.md` (secao 1b), nas skills `03-model-router` (secao 2b) e `19-run-ticket` (secao 2c), em `docs/MODEL_SELECTION_GUIDE.md` e e implementada em `core/line/subagent_routing.py` com parametros em `.factory/config/subagent_model_routing.json`. A politica vale para SUBAGENTES do harness; o cascade da esteira (`core/line/routing.py`, `.factory/config/line_routing.json`) nao muda.

## 1. AGENTS.md: novo bullet na secao "Execucao Canonica de Tickets e Roteamento Obrigatorio (Skill 19-run-ticket)"

Logo depois do bullet **Harness de Operacao como Desenvolvedor Principal**, acrescentar (uma unica linha):

```
- **Roteamento de Subagentes de Desenvolvimento (Haiku 5.5)**: o subagente de codigo usa Sonnet (`claude-sonnet-5-5`) por padrao e Opus (`claude-opus-5-5`) em planning; o Haiku 5.5 (`claude-haiku-5-5`) so e elegivel se TODAS valerem: tipo mecanico (mechanical_edit, docs_sync, test_from_spec, config_data, boilerplate_from_template, test_run_distill, ledger_update), complexidade `low` (`medium` apenas para docs_sync e test_run_distill), no maximo 3 arquivos e 150 linhas alteradas, criterio de aceite executavel pre-definido pelo planejador, ambiguidade resolvida (Gate G1) e zero falhas anteriores do Haiku na tarefa. O Haiku e PROIBIDO em caminhos protegidos por governanca, em tags de risco (security, credentials, auth, payments, concurrency, locking, transactions, migration, data_deletion, public_contract, routing, quota, flaky_test, root_cause_debug, architecture) e nos estagios planning, grill, review e integration (nunca planeja, revisa nem resolve conflito de integracao). Escalonamento: UMA tentativa com Haiku; se o portao focado falhar ou a revisao apontar defeito de correcao, nova tentativa com Sonnet (sem iterar 3 vezes com Haiku); o Haiku nunca escala sozinho para Opus nem Fable e `forbidden_autonomous_models` continua proibido. Piso de cota de 15.0% (mesma conta Anthropic), revisao em familia diferente do implementador (o revisor nunca e Haiku) e o portao unico `python C:\dev\DarkFac\core\harness\runner.py --quick` permanecem. Implementacao: `core/line/subagent_routing.py` e `.factory/config/subagent_model_routing.json`.
```

## 2. FACTORY_RULES.md: nova regra numerada 13

Depois da regra 12 ("Sincronizacao Continua Multi-Ambiente (USR-57)"), acrescentar (uma unica linha):

```
13. Roteamento de Subagentes de Desenvolvimento (Haiku 5.5, USR-168): O modelo do subagente de desenvolvimento do harness segue `core/line/subagent_routing.py` (parametros em `.factory/config/subagent_model_routing.json`): Sonnet (`claude-sonnet-5-5`) por padrao, Opus (`claude-opus-5-5`) em planning e Haiku 5.5 (`claude-haiku-5-5`) somente para fatias mecanicas (mechanical_edit, docs_sync, test_from_spec, config_data, boilerplate_from_template, test_run_distill, ledger_update) de complexidade baixa (media apenas em docs_sync e test_run_distill), ate 3 arquivos e 150 linhas, com criterio de aceite executavel pre-definido, ambiguidade resolvida (Gate G1) e nenhuma falha anterior do Haiku. E proibido usar Haiku em caminhos protegidos, tags de risco (security, credentials, auth, payments, concurrency, locking, transactions, migration, data_deletion, public_contract, routing, quota, flaky_test, root_cause_debug, architecture) e nos estagios planning, grill, review e integration. Havera no maximo UMA tentativa com Haiku; falha do portao focado ou defeito de correcao apontado na revisao escala para Sonnet, e o Haiku nunca escala sozinho para Opus ou Fable. A regra nao relaxa o piso de cota de 15.0%, a revisao em familia diferente do implementador nem o portao unico de validacao.
```

## 3. core/harness/test_subagent.py: trocar o Haiku 3.5 pelo Haiku 5.5

`core/harness/*` e protegido (commit humano). Em `core/harness/test_subagent.py`, ramo final do seletor de runner (linhas ~139-143), trocar:

```
                model_id="anthropic/claude-3.5-haiku",
```
por:
```
                model_id="anthropic/claude-haiku-5-5",
```

e, nas duas linhas seguintes do mesmo bloco, trocar:

```
                rationale="Claude 3.5 Haiku: Subagent standard for lightweight, ultra-low latency command execution and output distillation.",
                config_snippet='claude --model claude-3-5-haiku-latest (subagent in .claude/agents/test-runner.md)',
```
por:
```
                rationale="Claude Haiku 5.5: Subagent standard for lightweight, ultra-low latency command execution and output distillation.",
                config_snippet='claude --model claude-haiku-5-5 (subagent in .claude/agents/test-runner.md)',
```

Impacto em testes: `tests/test_implementar_execu_o_de_te.py` (linhas ~267 e ~291) afirma `anthropic/claude-3.5-haiku` e `claude-3.5-haiku` no prompt. Atualize essas asserts para `anthropic/claude-haiku-5-5` e `claude-haiku-5-5` no MESMO commit (esse teste nao e protegido, mas precisa acompanhar a troca). O `.claude/agents/test-runner.md` ja foi atualizado para `model: claude-haiku-5-5`.

## 4. Aplicacao

1. Abra `C:\dev\DarkFac\AGENTS.md` e `C:\dev\DarkFac\FACTORY_RULES.md` e faca as alteracoes das secoes 1 e 2; em `C:\dev\DarkFac\core\harness\test_subagent.py` faca a da secao 3 e ajuste `C:\dev\DarkFac\tests\test_implementar_execu_o_de_te.py`. Edite so essas linhas: nao mexa nos titulos e mantenha cada bullet/regra numa unica linha.
2. Antes de commitar, rode `python -m pytest tests/test_governance_guard.py tests/test_harness_contract.py tests/test_ci_policy.py tests/test_implementar_execu_o_de_te.py -q` em `C:\dev\DarkFac`: o push vai direto para a `main` (sem CI de PR antes).
3. Commite SOMENTE os arquivos desta proposta (`git add` com caminhos explicitos; nunca `git add -A`, o checkout compartilhado pode ter alteracoes locais) e de push direto na `main`. Nao use PR: o job `trusted-pr-policy` do CI roda o `guard.py` e reprova qualquer PR que toque arquivo protegido.
4. O `guard.py` NAO verifica autoria: ele so compara os caminhos protegidos com a base e reprova sempre que houver diferenca (`GUARD VIOLATION`), quem quer que tenha commitado; so passa com `DARKFAC_ALLOW_GOVERNANCE_EVOLUTION=1`. Por isso, rodar `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` ANTES do push acusa violacao (esperado, nao e erro). Depois do push o diff com `origin/main` some e o resultado e `[GUARD PASS]`.
