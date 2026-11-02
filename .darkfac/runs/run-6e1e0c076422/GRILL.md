# GRILL

## Premissas
- A rodada é exclusivamente de leitura e grill, sem escrita, criação de tickets ou operações externas.
- A demanda é uma evolução pontual da fábrica existente, demonstrada inicialmente em darkfac, sem ampliar permissões de outros projetos.
- O verificador reutilizará os contratos existentes de revisão, validação, integração, release e control store; um módulo novo dependerá de necessidade técnica comprovada.
- O vínculo das evidências distinguirá candidato revisado, commit de merge e artefato implantado, comprovando a correspondência entre eles mesmo quando houver squash.
- O aceite exigirá revisão aprovada por outra família, runner --quick com [HARNESS_PASS] e checagens executadas, lint-imports, complexity_ratchet, docs_sync, CI verde, merge confirmado, deploy e smoke reais e rechecagem persistida de pelo menos um achado real após correção.
- Evidência ausente, incoerente, de outro candidato ou substituída apenas por simulação manterá o aceite pendente, com motivo explícito; alteração do candidato exigirá revalidar os vínculos afetados.
- Manual, FASE_ATUAL e learning pack integrarão a mesma entrega, com rastreabilidade ao candidato e aos resultados operacionais, sem apresentar resultados futuros como já comprovados.
- A prova abrangerá os três domínios conforme as premissas fornecidas; indisponibilidade, desativação, cobertura insuficiente e adiamento não contarão como sucesso.
- O aceite automático respeitará as exceções vigentes de aceite comercial e a autorização assinada para arquivos protegidos, sem alterar governança nesta demanda.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
