---
name: autonomous-piv-loop
description: Executa tickets, funcionalidades e correções pelo ciclo Prime-Plan-Implement-Validate (PIV), com protocolo fail-closed para branches e worktrees concorrentes.
---

# Autonomous PIV Loop

O ciclo PIV divide a entrega em mudanças pequenas e verificáveis: preparar o contexto,
planejar, implementar uma unidade, validar imediatamente e só então avançar.

## Isolamento de contexto e estado

- Contexto fresco não substitui isolamento Git.
- Uma frente única usa branch dedicada. Cada frente concorrente que escreve recebe
  branch, worktree, owner e lease exclusivos.
- O checkout de integração é coordenado por um único owner e não é executor.
- Nunca use `startingState: working-tree` para propagar uma baseline suja.

Ao despachar duas ou mais frentes, receber uma entrega de outra worktree ou integrar
commits concorrentes, leia e siga integralmente
[references/worktree-parallelism.md](references/worktree-parallelism.md). O protocolo
é fail-closed: prova ausente de baseline, ownership, ambiente, heartbeat, conclusão ou
integração bloqueia a próxima transição.

## Loop por tarefa

1. Leia `AGENTS.md`, `MISSION.md` e `FACTORY_RULES.md`; identifique a origem do ticket pela tag canônica:
   - `user-demand` (`USR-XX`): Demanda manual aberta pelo usuário/dono do projeto (prioridade de escopo).
   - `code-review`: Ticket aberto por auditoria de revisão adversarial ou inspeção de qualidade.
   - `agent-feature`: Melhoria autônoma ou refatoração proposta pelos próprios agentes (self-improvement).
   Registre ticket, identidade, escopo e comandos de aceitação.
2. Faça o preflight da unidade de trabalho e do ambiente antes da primeira escrita.
   Valide o ambiente de terminal (`python core/harness/terminal_env.py --check` ou `scripts/init_terminal.ps1`),
   assegurando ancoragem na raiz e imunidade contra interferências de perfis (`-NoProfile`).
   Quando o ticket introduz o próprio teste focal, registre sua ausência esperada e
   prove coleta não vazia da suíte existente; não execute um caminho ainda inexistente.
3. Implemente somente os caminhos possuídos pela tarefa.
4. Execute o teste focal e `python core/harness/runner.py --quick`; corrija a causa
   antes de avançar se algum gate falhar.
5. Ao concluir, execute os comandos obrigatórios do repositório e produza o contrato
   de conclusão definido no protocolo.

## Relatório

Registre em `.factory/reports/<task-slug>-report.md`: ticket, owner, branch, worktree,
SHA-base e SHA final; arquivos alterados; comandos, contagens e exit codes; heartbeat
final; e estado residual. Uma resposta textual sem commit seletivo e evidência não é
handoff verificável.

## Continuous Self-Improvement e RCA

- Em follow-up corretivo, registre a intenção e o gap no motor de aprendizagem antes
  de implementar.
- Toda falha de validação, ferramenta, setup, quota, lease ou handoff recebe RCA antes
  do retry. Retry não cria silenciosamente uma nova identidade ou outro escritor.
- Uma regra preventiva só é considerada resolvida depois de um gate determinístico.
