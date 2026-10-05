# Relatório de Conclusão de Ticket: USR-116

**Ticket ID**: USR-116  
**Título**: run_ticket longo mistura versões de código: importa módulos tarde de um checkout compartilhado que outra sessão atualizou e a entrega quebra depois do portão aprovado  
**Data**: 2026-10-06  
**Status**: Completed  
**Branch**: `feature/usr-116-run-ticket-resume-delivery`  

---

## 1. Contexto e Causa Raiz

Durante execuções longas do launcher `run_ticket.py` (30 a 95 minutos), o processo Python mantinha módulos essenciais em memória (ex.: `core.demands.models.UserTicket`) carregados na inicialização a partir do checkout compartilhado `C:\dev\DarkFac`. Se outra sessão ou pull atualizasse o checkout em disco adicionando novos campos ou contratos (como ocorreu no incidente do USR-108 com o campo `delivery_evidence`), a fase de entrega do `run_ticket` importava tardiamente `from core.git.autonomy import GitAutonomyManager`.
Isso resultava em duas versões concorrentes do código no mesmo processo: o `GitAutonomyManager` recém-importado usava o novo contrato, enquanto `UserTicket` em memória era a versão defasada, causando exceções como `ValueError: "UserTicket" object has no field "delivery_evidence"`.

Além disso, não havia um comando canônico para retomar a entrega de uma worktree desenvolvida e preservada sem recorrer a scripts avulsos ou intervenção manual.

---

## 2. Solução Implementada

1. **Fase de Entrega em Processo Novo e Isolado (Critério 1)**:
   - Ao concluir a etapa de desenvolvimento do agente, `run_ticket.py` dispara a entrega (`--resume-delivery`) em um subprocesso Python inteiramente novo (`sys.executable`).
   - O novo processo importa todos os contratos, modelos e managers diretamente do estado atual do disco, eliminando qualquer risco de discrepância ou `ValueError` por módulos obsoletos em memória.

2. **Comando Oficial `--resume-delivery` (Critério 2)**:
   - Implementado e documentado em `run_ticket.py --help`:
     `python C:\dev\DarkFac\run_ticket.py --resume-delivery <TICKET_ID> [--worktree <caminho>]`
   - Suporta busca automática da worktree ativa mais recente do ticket via `find_ticket_worktree(ticket_id, PROJECT_ROOT)` em `core/git/ticket_workspace.py`.

3. **Rebase Automático Preventivo em `origin/main` com Tratamento de Conflitos (Critério 3)**:
   - Implementada a função `rebase_on_origin_main(cwd)`.
   - Antes de rodar a validação demorada e a entrega, o branch da worktree é rebaseado em `origin/main`.
   - Se houver conflito: lista claramente os arquivos conflitantes via `git diff --name-only --diff-filter=U`, aborta o rebase de forma limpa (`git rebase --abort`) mantendo a worktree intacta, imprime o comando `--resume-delivery` para retomada e sai com código != 0.

4. **Diagnóstico Estruturado e Preservação de Worktree em Falhas (Critério 4)**:
   - Se a entrega ou portão falhar após o desenvolvimento, o launcher preserva a worktree e imprime claramente:
     - Branch ativo
     - Commit SHA atual
     - Caminho absoluto da worktree
     - Comando exato `python C:\dev\DarkFac\run_ticket.py --resume-delivery <TICKET_ID> --worktree <caminho>`
   - Retorna código != 0 sem remover a worktree.

5. **Varredura e Limpeza Pós-Entrega**:
   - Após a conclusão bem-sucedida da entrega, aciona `git_mgr.sweep_stale(cwd=PROJECT_ROOT)` para limpar worktrees e branches já mergeadas.

---

## 3. Testes Determinísticos Realizados
- `tests/test_run_ticket_delivery.py`:
  - `test_resume_delivery_arg_parsing`: parsing correto de argumentos CLI.
  - `test_find_ticket_worktree`: localização determinística de worktrees ativas por mtime.
  - `test_rebase_on_origin_main_clean`: rebase limpo bem-sucedido.
  - `test_rebase_conflict_detects_files_and_aborts_cleanly`: detecção de conflitos, listagem de arquivos e abort limpo.
  - `test_resume_delivery_rebase_conflict_cli_output`: emissão de mensagem de conflito e comando de retomada.
  - `test_resume_delivery_failure_after_gate_prints_diagnostics`: impressão de branch, SHA, worktree e comando ao falhar entrega.
  - `test_resume_delivery_happy_path`: fluxo completo de sucesso com sweep.
  - `test_delivery_in_fresh_process_prevents_stale_module_value_error`: garantia de isolamento em novo processo.
- Todos os 18 testes de `run_ticket` aprovados com 100% de sucesso.
