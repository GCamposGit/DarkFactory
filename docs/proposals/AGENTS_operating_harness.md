# Proposta de texto para o AGENTS.md: harness de operacao como desenvolvedor principal (USR-109)

`AGENTS.md` e protegido por `core/orchestrator/guard.py` ("These files can ONLY be modified by a direct human commit"). Por isso a fabrica NAO o edita: este arquivo guarda o texto exato para o owner aplicar num commit proprio. A mesma regra ja esta em vigor em `CLAUDE.md` e nas skills `19-run-ticket` (secao 2b) e `03-model-router`, e implementada em `core/line/routing.py::pick` e `core/line/operating_harness.py`.

## 1. Corrigir o exemplo de harness eleito (defeito de documentacao)

Na secao "Execucao Canonica de Tickets e Roteamento Obrigatorio (Skill 19-run-ticket)", bullet **Bloqueio de Quota Critica no Chat**, trocar:

```
delegar para o harness saudavel eleito (ex.: Antigravity)
```

por:

```
delegar para o harness saudavel eleito para desenvolvimento (o roteador so elege harnesses com modo `write`: Claude, Codex e Grok Build; o Antigravity e somente leitura e nunca e eleito para `development`)
```

Motivo: `pick('development')` nunca devolve Antigravity (ele declara apenas `read` em `core/line/agent_cli.py::HARNESS_CAPABILITIES`); o preflight de 2026-10-01 elegeu Codex.

## 2. Acrescentar o bullet do harness de operacao

Logo depois do bullet **Bloqueio de Quota Critica no Chat**, acrescentar:

```
- **Harness de Operacao como Desenvolvedor Principal (USR-109)**: se o usuario opera a fabrica por um harness especifico (variavel `DARKFAC_OPERATING_HARNESS` ou autodeteccao pelo ambiente do harness pai), esse harness e eleito como desenvolvedor do estagio `development` sempre que sua cota estiver acima do piso critico de 15.0% (vence o maior headroom e a preferencia por faixa de complexidade). Se estiver critico, em cooldown, com cota desconhecida ou sem modo `write`, vale o Dynamic Headroom. A preferencia nunca relaxa o piso de 15.0%, nao altera grill/planning/review/integration (a revisao continua em outra familia que o implementador) e nao impede subagentes mais simples nem testes no desktop.
```

## 3. Outro arquivo protegido com o mesmo defeito: core/harness/suite_lock.py

`core/harness/*` e protegido (commit humano). `_detect_harness()` (por volta da linha 105) procura `GROK_CLI`, que o Grok Build 1.0.46 NAO exporta; o que ele exporta e `GROK_AGENT` e `GROK_SESSION_ID` (verificado em sessao real em 2026-10-01). Efeito: o rotulo do harness no lock de testes fica vazio quando o Grok opera a fabrica. Troca proposta, na tupla de pares `(variavel, rotulo)`:

```
("GROK_AGENT", "grok"),
("GROK_SESSION_ID", "grok"),
("GROK_CLI", "grok"),
```

(manter `GROK_CLI` por compatibilidade). A deteccao de roteamento ja usa a tabela correta em `core/line/operating_harness.py::AUTODETECT_ENV_SIGNALS`.

## 4. Aplicacao

1. Abra `C:\dev\DarkFac\AGENTS.md`, faca as alteracoes das secoes 1 e 2 e, se quiser corrigir o rotulo do lock de testes, a da secao 3 em `C:\dev\DarkFac\core\harness\suite_lock.py`; commite diretamente (commit humano).
2. Rode `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` apenas para confirmar que os unicos arquivos protegidos alterados sao esses, por autoria humana.
