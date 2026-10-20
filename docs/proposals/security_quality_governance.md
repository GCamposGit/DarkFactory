# SQ-03 — evolução legítima da governança

Ticket canônico: **USR-227**. Escopo: proposta documental DarkFac, autorizada pelas decisões D-08/P-05 de USR-191. Esta proposta não concede permissões nem altera verificadores. Inventário observado na base `145c5b7808cb`; reler a base confiável antes de qualquer implementação futura.

## Inventário verificável

| Fonte | Comportamento observado |
|---|---|
| `core/orchestrator/guard.py` | `PROTECTED_PATTERNS` e `audit_paths` identificam caminhos protegidos; `get_modified_files` inclui rastreados, não rastreados e ambos os lados de renomeações. Erro Git bloqueia a decisão. |
| `.github/workflows/ci.yml` / `trusted-pr-policy` | Em `pull_request_target`, executa o guard da base confiável contra o candidato, sem executar código candidato nesse job. Credenciais persistentes do checkout ficam desabilitadas. |
| `.github/workflows/ci.yml` / `pr-tests` + `pr-validation` | Desde o USR-300 (05cb6f4): `pr-tests` roda em Linux e em 4 fatias no Windows, aplica o guard da base e materializa `core/harness/` e `harness.config.json` da base antes de executar o portão; `pr-validation (ubuntu-latest)` e `pr-validation (windows-latest)` agregam esses resultados com os nomes dos checks obrigatórios. PRs só de ledger/docs passam pelo caminho `ledger-fast`, sem pular o guard. |
| `harness.config.json` | `syntax_and_types` executa `compileall`: verifica sintaxe, sem demonstrar checker de tipos. Reutilizar **USR-173 / SQ-08** para esta evolução futura. |
| `.agents/skills/19-run-ticket/SKILL.md` | Preflight, candidato isolado, portão único e revisão de família diferente; nenhuma evidência de PASS sobrevive a candidato alterado. |

O conjunto protegido atual é: `AGENTS.md`, `MISSION.md`, `FACTORY_RULES.md`, `FACTORY_GOVERNANCE.md`, `harness.config.json`, `.github/workflows/*`, `.agents/rules/*`, `core/harness/*`, `core/orchestrator/guard.py`, `.factory/holdout/*`, `tests/test_ci_policy.py`, `tests/test_governance_guard.py` e `tests/test_harness_contract.py`. Um padrão protege também um arquivo futuro; sua presença na lista não prova que o arquivo existe.

## Autoridade e percurso proposto

1. Um ticket próprio descreve motivo, arquivos exatos, comportamento antes/depois, riscos, testes negativos, migração e rollback. A proposta permanece em `docs/` enquanto a autorização de mudança protegida estiver pendente.
2. O Owner decide explicitamente o escopo protegido e a autoridade de aplicação. Registrar a decisão e seus tickets no backlog de ações humanas quando ela for necessária; aprovação desta documentação não autoriza uma alteração protegida futura.
3. Um planejador confronta a proposta com o guard e o CI da base confiável. O build escreve somente a candidata isolada; não pode editar a base que o julga, instalar seu próprio verificador ou declarar sua exceção válida.
4. Um revisor independente, de família diferente do implementador, examina o diff sanitizado, critérios e cenários negativos. Identificar executor e destino autorizado; material interno/restrito não pode ser exportado para um destino não especificado. Revisão local da mesma família é feedback, sem substituir a revisão requerida.
5. A aplicação de arquivos protegidos segue a autoridade humana exigida pelo guard vigente ou uma rota formal de executor confiável previamente autorizada e implementada em ticket próprio. Se a base não oferecer essa rota, bloquear a implementação protegida; não desabilitar guard/CI para entregá-la.
6. Fixar SHA/tree, base confiável, versão dos verificadores, identidade do revisor e digests dos artefatos. Executar o portão único, CI e negativos; mudanças, rebase ou push invalidam evidência anterior até nova verificação. Aprovação de registro de backlog não certifica a feature.
7. Integrar e implantar somente com evidência válida. Confirmar SHA em `origin/main`, resultado de cada serviço e smoke aplicável. Falha, timeout, testes não descobertos ou revisão ausente mantêm o ticket bloqueado.

O helper atual `is_governance_evolution_authorized` aceita uma variável de ambiente; não verifica por si um ticket ou decisão do Owner. Esse mecanismo observado não é prova suficiente de autorização. Esta proposta não o aciona. A lacuna está registrada em **USR-228**, para tratamento antes de promover uma rota autônoma protegida.

## Rollback e critérios negativos

Rollback usa um candidato de reversão rastreável ao último estado confiável, preservando auditoria e decisões. Reversão de arquivo protegido obedece à mesma autoridade e à base confiável; não restaura permissões silenciosamente. Se houver incidente, interromper a promoção e registrar o defeito antes de retomar.

Os testes futuros devem reprovar candidato que substitui seu guard, remove check obrigatório, renomeia arquivo protegido para escapar, apresenta base inválida, altera o SHA após revisão ou fornece apenas uma flag sem autorização verificável. Devem aceitar uma mudança documental sem arquivo protegido e uma evolução formalmente autorizada por executor independente. Esses cenários são critérios futuros, não testes executados por este documento.

## Saída desta fatia

Entrega: inventário e handoff. Nenhum scanner, agendador, campanha, alteração de skill, checker ou privilégio é ativado. SQ-02 continua responsável pelos perfis/threat model; USR-170/171 pelo vínculo de evidência/revisão, USR-172 por privilégios e USR-173 por tipos. Seus estados reais devem ser reconciliados antes do consumo.
