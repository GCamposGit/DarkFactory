# Handoff operacional

Versao: 12
Atualizado em: 2026-11-11 (Claude Code, coordenador de 09/11 a 11/11)

## Estado

- Fase: V0 da Dark Factory (0/5 entregas qualificadas; piloto M1 bloqueado na OA-082).
- `main` em `14d8d7ef` (#848), CI verde, Hub em producao no mesmo sha da ultima verificacao.
- Despachante: tarefa agendada `DarkFac-Fleet-Dispatcher` (loop) com `--recovery-tickets USR-510,USR-509,USR-508,USR-506,USR-498,USR-503,USR-499,USR-500,USR-502,USR-492,USR-483,USR-504`. Desde o USR-508 ele se reinicia sozinho quando o `main` muda.
- Hosts: Desktop executa; Grokbox oscila (cai e volta); Notebook frequentemente com `workers=0` porque a RAM livre fica abaixo de 4 GiB com os apps do owner abertos.
- Cotas (11/11 ~06:40): Claude semanal 6% (abaixo do piso), Codex 15% (no piso, renova 12/11 09:58 BRT), Grok ~21% e caindo rapido (owner confirmou saldo oficial em 10/11 e autorizou uso a vontade). Abaixo do piso, a escrita vai para o OpenRouter (teto US$ 2 por execucao, US$ 10 por mes).
- Revisao: cascata do USR-488 (outra familia por harness; outra familia via OpenRouter; mesma familia com outro modelo; mesmo modelo com persona adversarial). Admissao compara familia do modelo, nao nome do harness (#834).

## Decisoes do owner em vigor (09-10/11)

- Prioridade: funcionamento estavel, autonomo e paralelo; corrigir na hora o que for pequeno, ticket para o resto, reaproveitando tickets do roadmap.
- OpenRouter e fallback de cota baixa; revisao na mesma familia so sem alternativa.
- Grok preferido para escrita enquanto houver saldo.
- Politica de portao: PR com testes afetados e contratos; suite completa em Linux ou CI; CI noturno Windows permanece. Ainda depende da assinatura da OA-089 para entrar no `AGENTS.md` (PR #832).
- Subagentes leves para tarefas simples e leitura de testes; o coordenador recebe so resumo e acoes.

## O que foi entregue (09-11/11)

- Correcoes feitas pela coordenacao via subagente: #831 e #834 (USR-488, cascata de revisao e familia do modelo), #833 (estado runtime no `.gitignore`), #835/#836/#837/#838 (espera 75, trava de linha, reserva de frota e PR ativo nao contam como falha), #839 (falha repetida segura so o ticket; `set_recovery` saiu de `_record_failure`), #830 (plano Frente 7).
- Entregas da propria frota, sem toque humano: USR-501 (#841, sentinela de fila ociosa), USR-497 (#842, fechar falhas quando o `main` muda), USR-494 (#843, via expressa de autorreparo), USR-508 (#848, despachante reinicia ao mudar o `main`).
- Varredura de 09/11: 159 worktrees removidas (84 commits soltos preservados em tags `archive/worktree-*`), 5 PRs antigos fechados, 6 tickets orfaos devolvidos a fila.

## O que esta em voo

- USR-510 (Desktop): admissao deve considerar todos os workers de portao, nao so o local. E o maior gargalo de vazao: com o Notebook sem RAM, todo lancamento sai com `gate_capacity_zero` (`gate://local`).
- USR-492 (Grokbox): ordem de hosts Notebook, Grokbox oportunista, Desktop.
- Em seguida na prioridade: USR-509 (entregas com PR de CI vermelho ficam `blocked deploy_evidence_missing` e nao corrigem: USR-447 #845, USR-449 #846, USR-493 #844, USR-486 #847), USR-506 (motivo de falha grava trecho de codigo).

## Pendencias

- OA-089 (owner): assinar a autorizacao do `AGENTS.md` (secoes 3 e 5). Passo a passo no DarkHub.
- PRs antigos #825 (USR-468), #822 (USR-448) e #821: com conflito e CI vermelho; decidir retomar ou fechar.
- Tickets abertos relevantes: USR-496 (ids de gateway do Grok), USR-498..USR-504, USR-506, USR-509, USR-510.

## Armadilhas novas

1. Recusa nao e falha: saidas 75, `[TRAVA DE LINHA]`, `[RESERVA DE FROTA]` e "ja possui PR ativo" sao espera e nao devem acender recuperacao. Se surgir outro tipo de recusa contando como falha, adicione o marcador em `LOCK_MARKERS` de `core/line/fleet_dispatcher.py`.
2. Com recuperacao ativa, `run_ticket.py` usa teto de frota 1; a recuperacao global so deve ser acionada explicitamente.
3. Cota do Grok: queda de uso no mesmo periodo e marcada como leitura suspeita e o roteador exclui o Grok. O mecanismo existente de confirmacao e `.factory/usage/history/xai.confirmation.json` (quota_id, period_start, used_percent da leitura atual). USR-504 trata de forma duravel.
4. `admission.sqlite3`: falhas antigas de causa ja corrigida podem ser fechadas (`update failures set open=0`) e `dispatcher_recovery.active=0`, sempre com backup em `.factory/backups/`. O USR-497 automatiza o fechamento quando o `main` muda.
5. CI de PR leva de 3 a 5 minutos para criar o run do `DarkFac CI`; antes disso so aparece `trusted-pr-policy`.
6. `gh pr close` pode ser negado pelo classificador de permissoes; peca autorizacao explicita ao owner.
7. Id literal de modelo neste arquivo quebra o censo de modelos (baseline 0). Use alias.

## Proximo passo

- Acompanhar a entrega do USR-510 e, em seguida, do USR-509; com os dois, a frota deve rodar nos tres hosts e retomar as entregas presas.
- Quando o Grok cair abaixo de 15%, conferir que a escrita passa ao OpenRouter sem travar e que o gasto respeita o teto mensal.
- Retomar a V0 depois da Fase 1 do plano (USR-497..USR-503).
