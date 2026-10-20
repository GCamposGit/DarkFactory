# Handoff SQ-03 / USR-227

Referência: [proposta de governança](../proposals/security_quality_governance.md). Escopo aprovado nesta fatia: dois documentos, sem modificar arquivos protegidos. A autorização USR-191 permite documentar a rota, não aplicar exceções futuras.

## Contrato para o próximo executor

- Revalidar `origin/main`, ticket, claims disponíveis e diffs de workers concorrentes. Reservar caminhos em ticket/worktree própria; presença de PR ou silêncio não prova ownership.
- Ler o guard e CI pertencentes à base confiável. Registrar SHA completo da base/candidato e arquivos permitidos. Nunca usar candidato como fonte da regra que o aprova.
- Para mudança protegida, obter decisão explícita sobre arquivos, executor e rollback; registrar ação do Owner no backlog oficial se ainda necessária. Não transformar essa decisão futura em autorização implícita deste documento.
- Revisor de outra família deve produzir evidência do mesmo candidato, com identidade e destino conhecidos. Diferença de SHA/tree, material indisponível ou exportação não autorizada bloqueia revisão.
- Reutilizar USR-173 para introdução de tipos; `compileall` sozinho não é checker. Nenhuma alteração incidental em `core/harness/`, workflows, guard ou skills.
- Tratar formalmente o helper de autorização por ambiente antes de confiar nele como prova de decisão. Flag isolada não demonstra ticket, aprovador ou candidato.

## Validação e encerramento

Conferência documental: referências existentes, lista de caminhos igual à base inspecionada, diff limitado aos documentos e ausência de configuração/ativação. Não escrever testes que apenas espelham texto.

Portão oficial: `python core/harness/runner.py --quick` em candidato isolado imutável; para execução manual usar `scripts/gate_isolated.py --ref <SHA>` conforme Skill 19. Não iniciar uma segunda suíte integral separada. `[HARNESS_PASS]` exige checks executados e vínculo intacto; timeout/invalidação não são PASS.

Após revisão independente e portão: commit atômico `[USR-227]`, PR, CI, merge autônomo, confirmação em `origin/main`, deploy por `scripts/dokploy_redeploy.py` e resultado por serviço. Registrar conclusão somente com efeitos verificáveis; em bloqueio preservar candidata, motivo e ticket de defeito existente ou novo.

## Estado da evidência

Este arquivo não registra PASS, PR integrado ou deploy. A evidência operacional pertence ao run oficial e ao ledger, ligados ao SHA efetivamente validado. A publicação destes documentos não prova implementação dos controles futuros.
