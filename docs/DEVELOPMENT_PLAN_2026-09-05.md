# Plano de desenvolvimento da Dark Factory — 05/09/2026

## Trilha operacional de paralelismo

| ID | Escopo | Dependências | Critérios de aceitação e validação |
| --- | --- | --- | --- |
| DF-23 | `.agents/skills/04-autonomous-piv-loop/`, espelho `.claude`, teste de contrato e aprendizagem | DF-02, DF-04, DF-11 | Antes de qualquer novo bloco funcional, o protocolo fail-closed exige checkpoint de integração limpo e backup recuperável; fingerprint de SHA/dirty-state; branch, worktree, owner e lease exclusivos; manifesto e lock de ownership sem sobreposição; IDs, títulos e cwd persistidos; preflight Python/pytest e heartbeat; conclusão com SHAs, arquivos, testes e estado residual; commit seletivo sem baseline herdada; handoff proibido para raiz dirty ou com escritor ativo; retry com RCA; integração topológica e validação conjunta; limpeza apenas com commits alcançáveis e gates verdes. `python -m pytest tests/test_worktree_protocol.py -v` |

DF-23 altera apenas as instruções e suas provas determinísticas nesta etapa. A futura
automação funcional de worktrees permanece um bloco separado e não começa antes de
este contrato estar validado.
