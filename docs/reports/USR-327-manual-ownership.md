# USR-327 — Readmissao durante execucao manual

Em 21/10/2026 08:46 UTC, health de producao confirmou e1a4110 e a projecao PostgreSQL mostrou run-ffb7c3cd7fd1/USR320 development pending apesar de PR512 entregue. O endpoint canonico cancelou somente esse duplicado (jobs_cancelled=1); o status runtime foi reconciliado completed. Evidencias fora git: .factory/test_logs/usr320/reconcile-completed.json e proofs-304306-fresh.json.

O guard consulta eventos manuais persistidos por projeto/ticket antes do feeder e do intake. Recarregar o ledger planned nao remove essa evidencia compartilhada. Eventos terminais liberam o guard; abandono usa ABANDON_AFTER_SECONDS existente (6h), sem TTL novo. Timestamp invalido e indisponibilidade geram recusa diagnosticavel. Ausencia da tabela em banco legado tem compatibilidade explicita.

Isto e um guard consultivo contra readmissao, nao uma lease ou lock distribuido: publishing e best-effort. Claims e reservas atomicos da linha continuam intactos. A corrida entre inicio manual ainda nao publicado e intake concorrente permanece delimitada em USR-330. O teste de dois leitores/coordenadores apos publicacao nao e prova de atomicidade dessa corrida.

Validacao focada: 24 passed em 1.84s (inclui paridade de query PostgreSQL mock, timeout e erro redigido) (manual_ownership, backlog_feeder, feeder_cycle). Intake publico afetado + expiracao + regressao: 42 passed em 49.75s, tree hygiene limpa. Portao oficial e revisao independente ainda pendentes; infraestrutura instavel (Grokbox offline e Desktop worker_unreachable em gates concorrentes), sem gate local por RAM livre4.36GB. USR327 nao deve ser declarado integralmente entregue antes desses controles.

Provas304/306: API publica postgres/ok mostrou2slots (grokbox279/notebook320duplicado), Desktopunknown; health direto Desktopok idle, Grokboxtimeout12s. Nao ha evidencia de tres ciclos simultaneos ou duas linhas feeder_cycle sucessivas. Permanece pendencia operacional dos proprios tickets.
