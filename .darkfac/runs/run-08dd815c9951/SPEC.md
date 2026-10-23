# SPEC

## Objetivo
Corrigir o roteador para que pick('development', [...]) no Notebook nunca devolva None quando existe harness com modo write acima do piso de 15%: (a) incluir Grok na cascata de development e registrar em log o motivo exato de cada harness descartado; (b) inverter o bloqueio do Codex headless no Windows para opt-out explicito; (c) impedir que um probe Anthropic 'bem-sucedido' porem sem medicao nova seja reportado como outcome=ok sem renovar o snapshot, tornando o diagnostico de cota honesto e rastreavel; (d) teste de regressao que reproduz o log de 22/10 (snapshot anthropic de ~115538 s, probe ok sem medicao, Grok com 17,4%).

## Fora de escopo
- Relaxar o piso de 15% de cota efetiva (permanece estrito: <= 15% e inelegivel)
- Forjar checked_at ou reutilizar cota antiga como se fosse nova quando o probe nao mede nada
- Alterar as cascatas de planning, grill, review, integration ou distill
- Alterar ranking de complexidade, preferencia do harness de operacao (USR-109), cooldown ou reservas de cota
- Expor credenciais, tokens ou saida bruta de probe em logs

## Desenho
Tres causas independentes do incidente. (1) `.factory/config/line_routing.json` tem development.cascade = [claude/sonnet, codex]; Grok so existe em harness_pins, entao `_resolve_cascade` nunca o produz e `_routable_candidates` o ignora sem log -- pick com apenas `harness:grok` devolve None mudo. Correcao: adicionar ["grok", "development.grok"] ao fim da cascata de development e instrumentar `_routable_candidates` (core/line/routing.py:546-586) com um log por descarte, cobrindo tambem os harnesses presentes em host_caps que nao estao na cascata (novo motivo `not_in_cascade`), alem de `excluded`, `missing_host_capability`, `no_write_mode`, `forbidden_model`; pick() ja loga critical/unknown e cooldown. (2) `codex_headless_blocked()` (routing.py:534-543) bloqueia por padrao no Windows; owner decidiu em 23/10/2026 que o bloqueio vale somente com opt-out explicito -- passa a retornar True apenas quando DARKFAC_CODEX_HEADLESS_ALLOWED tem valor negativo explicito (0/false/no/off); ausente ou afirmativo libera. (3) `ClaudeCodeAccountAdapter.inspect` (core/usage/adapters.py:1383-1398) chama o probe ao vivo e, quando ele devolve None ou um resultado sem medidor, cai em `self._from_snapshot(snapshot)` devolvendo o checked_at antigo; `run_with_deadline` (core/usage/probe_deadline.py:102) loga outcome=ok apenas porque nao houve excecao, produzindo a contradicao do log. Correcao: o probe ao vivo persiste o snapshot sempre que obtem medicao verificavel (ja faz em adapters.py:1639) e, quando nao obtem, inspect marca o retorno como evidencia antiga em vez de silencio; `run_with_deadline` passa a distinguir ok_measured de ok_sem_medicao usando o checked_at do resultado, e `_quota_headroom_diagnosis` (routing.py:405-475) emite uma linha unica por provider com provider, motivo, idade da evidencia e se o snapshot foi renovado. Nenhuma dessas mudancas toca o piso de 15% nem autoriza snapshot sintetico.

## Arquivos a tocar
- core/line/routing.py
- .factory/config/line_routing.json
- core/usage/adapters.py
- core/usage/probe_deadline.py
- tests/line/test_routing.py
- tests/test_usr336_hidden_windows.py
- tests/line/conftest.py
- tests/test_run_ticket_routing.py

## Riscos
- Incluir Grok na cascata de development muda o vencedor do Dynamic Headroom em hosts onde Grok tem mais headroom que Claude; mitigado por ele entrar no fim da cascata e o ranking continuar por headroom
- Liberar Codex por padrao no Windows pode reabrir as janelas de console do USR-336 em hosts sem opt-out; mitigado pela variavel negativa explicita documentada no log de bloqueio
- Mudar o texto do log de probe pode quebrar testes/consumidores que casam com 'outcome=ok'; mitigado mantendo o prefixo 'probe provider=... outcome=' e apenas refinando o valor
- Logar todo descarte pode poluir o log em hosts com muitos harnesses; mitigado usando nivel info e uma linha por candidato
- Testes dependentes de snapshots reais em .factory/usage/providers podem ficar flaky; usar tmp_path + DARKFAC_STATE_ROOT como os testes existentes
